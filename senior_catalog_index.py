# -*- coding: utf-8 -*-
"""Compact Senior product directory and on-demand derivation lookup queue.

This module does not communicate with Senior. A factory worker (or a manual
PowerShell command) consumes the queue via authenticated Flask API endpoints.
"""
import csv
import json
import os
import re
import secrets
import sqlite3
import unicodedata
from datetime import datetime, timezone

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.environ.get("WJ_SENIOR_INDEX_DB", os.path.join(DATA_DIR, "senior_product_index.sqlite"))
MAX_RESULTS = 30


def normalize(value):
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^A-Z0-9]+", " ", text.upper()).strip()


def canonical_brand(brand):
    val = normalize(brand)
    if val in ("WJ", "WALDIR JUNIOR"):
        return "WJ"
    if val in ("BELLA", "BELLA ITALIA"):
        return "BELLA"
    raise ValueError("Marca inválida: use WJ ou Bella Itália.")


def db():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=15000")
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript("""
    CREATE TABLE IF NOT EXISTS products (
        environment TEXT NOT NULL,
        brand TEXT NOT NULL,
        code TEXT NOT NULL,
        description TEXT NOT NULL,
        search_description TEXT NOT NULL,
        search_code TEXT NOT NULL,
        PRIMARY KEY (environment, brand, code)
    );
    CREATE INDEX IF NOT EXISTS idx_product_brand ON products(environment, brand, code);
    CREATE TABLE IF NOT EXISTS catalog_metadata (
        environment TEXT NOT NULL,
        brand TEXT NOT NULL,
        imported_at TEXT NOT NULL,
        source TEXT NOT NULL,
        total INTEGER NOT NULL,
        PRIMARY KEY(environment, brand)
    );
    CREATE TABLE IF NOT EXISTS derivation_jobs (
        job_id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        status TEXT NOT NULL,
        environment TEXT NOT NULL,
        brand TEXT NOT NULL,
        web_product TEXT NOT NULL,
        code TEXT NOT NULL,
        description TEXT NOT NULL,
        derivations TEXT,
        unidade TEXT,
        result_source TEXT,
        error TEXT,
        applied_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_jobs_status ON derivation_jobs(status, created_at);
    """)
    # Upgrade existing Phase 1 databases in-place. NEVER replace the index/queue DB:
    # it contains confirmed and pending jobs from the actual installation.
    columns = {row["name"] for row in con.execute("PRAGMA table_info(derivation_jobs)")}
    if "unidade" not in columns:
        con.execute("ALTER TABLE derivation_jobs ADD COLUMN unidade TEXT")
    return con


def import_csv(path, brand, environment="producao", source=None):
    """Replace a brand/environment slice atomically, preserving other environments."""
    brand = canonical_brand(brand)
    environment = validate_environment(environment)
    items = {}
    with open(path, encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        if not {"Produto", "Descrição"}.issubset(reader.fieldnames or []):
            raise ValueError("CSV exige colunas Produto e Descrição.")
        for row in reader:
            code, description = (row.get("Produto") or "").strip(), (row.get("Descrição") or "").strip()
            if not code or not description:
                continue
            if code in items and items[code] != description:
                raise ValueError("Código duplicado com descrições distintas: " + code)
            items[code] = description
    if not items:
        raise ValueError("Nenhum produto utilizável no CSV.")
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    con = db()
    try:
        with con:
            con.execute("DELETE FROM products WHERE environment=? AND brand=?", (environment, brand))
            con.executemany("""
                INSERT INTO products(environment, brand, code, description, search_description, search_code)
                VALUES (?, ?, ?, ?, ?, ?)
            """, ((environment, brand, code, desc, normalize(desc), normalize(code))
                   for code, desc in items.items()))
            con.execute("""
                INSERT INTO catalog_metadata(environment,brand,imported_at,source,total)
                VALUES(?,?,?,?,?)
                ON CONFLICT(environment,brand) DO UPDATE SET
                  imported_at=excluded.imported_at,source=excluded.source,total=excluded.total
            """, (environment, brand, stamp, source or os.path.basename(path), len(items)))
    finally:
        con.close()
    return len(items)


def validate_environment(environment):
    val = (environment or "producao").lower().strip()
    if val not in ("homolog", "producao"):
        raise ValueError("Ambiente deve ser homolog ou producao.")
    return val


def metadata():
    con = db()
    try:
        return [dict(r) for r in con.execute("SELECT * FROM catalog_metadata ORDER BY environment, brand")]
    finally:
        con.close()


def search(query, brand, environment="producao", limit=25):
    brand = canonical_brand(brand)
    environment = validate_environment(environment)
    query = normalize(query)
    if not query:
        return []
    tokens = query.split()[:8]
    # Parameterized SQL. Every meaningful query word must occur in the code/name.
    clauses = ["environment=?", "brand=?"]
    params = [environment, brand]
    for token in tokens:
        clauses.append("(search_description LIKE ? ESCAPE '\\' OR search_code LIKE ? ESCAPE '\\')")
        like = "%" + token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        params.extend([like, like])
    params += [query, query + "%", query + "%", min(MAX_RESULTS, max(1, int(limit)))]
    sql = """
      SELECT code, description FROM products WHERE %s
      ORDER BY CASE
        WHEN search_code=? THEN 0
        WHEN search_code LIKE ? THEN 1
        WHEN search_description LIKE ? THEN 2
        ELSE 3 END,
        LENGTH(description), description, code
      LIMIT ?
    """ % " AND ".join(clauses)
    con = db()
    try:
        return [dict(r) for r in con.execute(sql, params)]
    finally:
        con.close()


def find_product(code, brand, environment):
    con = db()
    try:
        r = con.execute("SELECT code,description FROM products WHERE code=? AND brand=? AND environment=?",
                        (code, canonical_brand(brand), validate_environment(environment))).fetchone()
        return dict(r) if r else None
    finally:
        con.close()


def get_job(job_id):
    con = db()
    try:
        row = con.execute("SELECT * FROM derivation_jobs WHERE job_id=?", (job_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["derivations"] = json.loads(result.pop("derivations") or "[]")
        return result
    finally:
        con.close()



def recent_jobs(limit=25):
    """Small admin-only status listing; results remain in the persisted SQLite DB."""
    limit = min(60, max(1, int(limit)))
    con = db()
    try:
        rows = con.execute("""
            SELECT job_id, created_at, updated_at, status, environment, brand,
                   web_product, code, description, result_source, error, applied_at,
                   derivations, unidade
            FROM derivation_jobs
            ORDER BY created_at DESC, rowid DESC LIMIT ?
        """, (limit,)).fetchall()
        return [{**{k: row[k] for k in row.keys() if k != "derivations"},
                 "derivation_count": len(json.loads(row["derivations"] or "[]"))}
                for row in rows]
    finally:
        con.close()


def latest_jobs_per_product():
    """Lightweight status summary for inline admin buttons: one newest job per web product.

    Full derivation arrays stay on the individual job endpoint, not this request.
    The summary is intentionally not limited to the last N jobs; after a refresh
    older product statuses must not silently disappear as more jobs are created.
    """
    con = db()
    try:
        return [dict(row) for row in con.execute("""
            SELECT job_id, web_product, brand, environment, code, status,
                   result_source, applied_at, updated_at
            FROM derivation_jobs
            WHERE rowid IN (
                SELECT MAX(rowid) FROM derivation_jobs GROUP BY brand, web_product
            )
            ORDER BY web_product, brand
        """)]
    finally:
        con.close()


def create_job(web_product, brand, code, environment="producao"):
    brand = canonical_brand(brand)
    environment = validate_environment(environment)
    item = find_product(code, brand, environment)
    if not item:
        raise ValueError("Código não encontrado no índice desta marca/ambiente.")
    con = db()
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        with con:
            existing = con.execute("""SELECT job_id FROM derivation_jobs WHERE
                web_product=? AND brand=? AND environment=? AND code=? AND status='pending'
                ORDER BY created_at DESC LIMIT 1""", (web_product, brand, environment, code)).fetchone()
            if existing:
                return get_job(existing["job_id"])
            jid = "DER-" + secrets.token_hex(10).upper()
            con.execute("""INSERT INTO derivation_jobs
              (job_id,created_at,updated_at,status,environment,brand,web_product,code,description)
              VALUES(?,?,?,'pending',?,?,?,?,?)""",
              (jid, stamp, stamp, environment, brand, web_product, code, item["description"]))
        return get_job(jid)
    finally:
        con.close()


def pending_jobs(limit=20):
    con = db()
    try:
        return [dict(r) for r in con.execute("""SELECT job_id,created_at,environment,brand,
            web_product,code,description FROM derivation_jobs WHERE status='pending'
            ORDER BY created_at ASC LIMIT ?""", (min(50, max(1, int(limit))),))]
    finally:
        con.close()


def complete_job(job_id, status, derivations=None, error="", source="mock", unidade=None):
    if status not in ("completed", "error"):
        raise ValueError("Status inválido: completed ou error.")
    if source not in ("mock", "senior_homolog", "senior_producao"):
        raise ValueError("Origem inválida.")
    if status == "completed":
        # unit of measure belongs to the product (uniMed), not each derivation.
        # A real Senior response must include it; don't silently retain a manual
        # placeholder such as "32" or an empty value in sapiens_map.json.
        if unidade is not None and not isinstance(unidade, str):
            raise ValueError("Unidade Senior deve ser texto (uniMed).")
        unit = (unidade or "").strip()
        if len(unit) > 16 or any(ord(c) < 32 for c in unit):
            raise ValueError("Unidade Senior inválida (uniMed).")
        if source != "mock" and not unit:
            raise ValueError("Senior não retornou uniMed; verifique o campo no F000CWS.")
        if not isinstance(derivations, list) or len(derivations) > 1000:
            raise ValueError("Derivações devem ser uma lista com no máximo 1000 itens.")
        clean = []
        seen = set()
        for d in derivations:
            if not isinstance(d, dict):
                raise ValueError("Derivação inválida.")
            code, name = str(d.get("cod", "")).strip(), str(d.get("nome", "")).strip()
            if not code or not name or len(code) > 80 or len(name) > 300:
                raise ValueError("Cada derivação precisa de código e descrição válidos.")
            if code in seen:
                raise ValueError("Código de derivação repetido: " + code)
            seen.add(code)
            clean.append({"cod": code, "nome": name})
    else:
        clean = []
        unit = ""
        error = str(error or "Consulta Senior falhou.")[:1000]
    con = db()
    try:
        with con:
            row = con.execute("SELECT * FROM derivation_jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise LookupError("Job não encontrado.")
            # Distinct environments cannot silently reuse one another's derivations.
            expected_source = "senior_homolog" if row["environment"] == "homolog" else "senior_producao"
            if source != "mock" and source != expected_source:
                raise ValueError("Origem do resultado não corresponde ao ambiente do job.")
            if row["status"] != "pending":
                # An already confirmed result is immutable, even on retries.
                if (row["status"] == status and row["result_source"] == source and
                    json.loads(row["derivations"] or "[]") == clean and (row["unidade"] or "") == unit and
                    (row["error"] or "") == (error if status == "error" else "")):
                    return get_job(job_id)
                raise RuntimeError("Job já concluído com outro resultado.")
            stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            con.execute("""UPDATE derivation_jobs SET status=?,updated_at=?,derivations=?,unidade=?,result_source=?,error=?
                WHERE job_id=? AND status='pending'""", (status,stamp,json.dumps(clean,ensure_ascii=False),
                                           unit,source,error if status == "error" else "",job_id))
        return get_job(job_id)
    finally:
        con.close()


def applied_mappings():
    """Only code mappings explicitly applied from real Senior results are verified."""
    con = db()
    try:
        results = {}
        rows = con.execute("""SELECT web_product,brand,code,environment,applied_at
                    FROM derivation_jobs WHERE applied_at IS NOT NULL
                    AND result_source IN ('senior_homolog', 'senior_producao')
                    ORDER BY applied_at ASC""").fetchall()
        for row in rows:
            results[row["web_product"]] = dict(row)
        return results
    finally:
        con.close()


def mark_applied(job_id):
    con = db()
    try:
        with con:
            con.execute("UPDATE derivation_jobs SET applied_at=? WHERE job_id=? AND status='completed'",
                        (datetime.now(timezone.utc).isoformat(timespec="seconds"), job_id))
    finally:
        con.close()
