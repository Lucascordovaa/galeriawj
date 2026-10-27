# -*- coding: utf-8 -*-
"""
Servidor local do sistema de orçamentos WJ / Bella Itália.
Rodar:  python app/server.py   (abre em http://localhost:8000)
"""
import csv
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import unicodedata
import hashlib
import tempfile
from datetime import date, datetime
from functools import wraps

from flask import (Flask, Response, jsonify, request, send_file,
                   send_from_directory, session)
from pypdf import PdfReader, PdfWriter
from reportlab.lib.colors import HexColor
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from werkzeug.security import check_password_hash, generate_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
STATIC = os.path.join(BASE, "static")
BACKUPS = os.path.join(ROOT, "backups")
AUTH_FILE = os.path.join(BASE, "admin_auth.json")
DATA_DIR = os.path.join(BASE, "data")
VENDEDORES_FILE = os.path.join(DATA_DIR, "vendedores.json")
LEADS_FILE = os.path.join(DATA_DIR, "leads.csv")
ITENS_FILE = os.path.join(DATA_DIR, "itens.csv")
ORC_DIR = os.path.join(DATA_DIR, "orcamentos")          # 1 arquivo JSON por orçamento salvo
ORC_CSV_FILE = os.path.join(DATA_DIR, "orcamentos.csv")  # export derivado p/ Power BI (status)
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(ORC_DIR, exist_ok=True)

# Prefixo de URL: vazio em desenvolvimento (localhost), "/orcamento" em produção.
# Definido pela variável de ambiente WJ_URL_PREFIX no servidor.
URL_PREFIX = os.environ.get("WJ_URL_PREFIX", "").rstrip("/")
BASE_HREF = (URL_PREFIX + "/") if URL_PREFIX else "/"

import catalog_builder
import sapiens
import senior_catalog_index as senior_index

CATALOG = None
PRODUCTS = None
SOURCE_PDF = None

def load_catalog():
    global CATALOG, PRODUCTS, SOURCE_PDF, _reader, _closing
    with open(os.path.join(STATIC, "catalog.json"), encoding="utf-8") as f:
        CATALOG = json.load(f)
    PRODUCTS = {p["id"]: p for p in CATALOG["products"]}
    SOURCE_PDF = os.path.join(ROOT, CATALOG["source"])
    _reader = None
    _closing = None

_reader = None
_closing = None
load_catalog()

PAGE_W, PAGE_H = 1440, 810  # mesmo formato do catálogo

# fonte Inter (pesos estáticos gerados por convert_inter.py)
FONTS_TTF = os.path.join(BASE, "fonts_ttf")
for _w in ("Regular", "Semibold", "Bold"):
    pdfmetrics.registerFont(TTFont(f"Inter-{_w}", os.path.join(FONTS_TTF, f"Inter-{_w}.ttf")))

app = Flask(__name__, static_folder=STATIC, static_url_path="/static")


# ----------------------------- autenticação admin -----------------------------
def load_auth():
    if os.path.exists(AUTH_FILE):
        try:
            with open(AUTH_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_auth(data):
    with open(AUTH_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)


# chave de sessão + token do Power BI (criados uma vez e guardados em admin_auth.json)
_auth = load_auth()
_changed = False
if "secret" not in _auth:
    _auth["secret"] = secrets.token_hex(32)
    _changed = True
if "powerbi_token" not in _auth:
    _auth["powerbi_token"] = secrets.token_urlsafe(24)
    _changed = True
if "relay_token" not in _auth:
    # token que o relay (agente on-premise) usa para autenticar no nosso sistema
    _auth["relay_token"] = secrets.token_urlsafe(32)
    _changed = True
if _changed:
    save_auth(_auth)
app.secret_key = _auth["secret"]

# Isola a sessão por instância: produção (/orcamento) e staging (/orcamento/v3)
# rodam no mesmo domínio; sem isso as duas usariam o mesmo cookie "session" e um
# login derrubaria o outro. Nome + caminho distintos por prefixo mantêm tudo separado.
if URL_PREFIX:
    app.config["SESSION_COOKIE_NAME"] = "wjsession_" + re.sub(r"[^a-z0-9]", "", URL_PREFIX.lower())
    app.config["SESSION_COOKIE_PATH"] = BASE_HREF


def password_set():
    return "hash" in load_auth()


def admin_logged_in():
    return session.get("admin") is True


def require_admin(f):
    @wraps(f)
    def wrapper(*a, **k):
        if not admin_logged_in():
            return jsonify({"error": "Não autorizado. Faça login no painel admin."}), 401
        return f(*a, **k)
    return wrapper


def current_vendedor():
    """Conta do vendedor logado na sessão (ou None)."""
    u = session.get("vendedor")
    v = find_vendedor(u) if u else None
    return v if (v and v.get("ativo", True)) else None


def require_vendedor(f):
    @wraps(f)
    def wrapper(*a, **k):
        if not current_vendedor():
            return jsonify({"error": "Faça login como vendedor."}), 401
        return f(*a, **k)
    return wrapper


def render_page(name):
    """Serve um HTML do /static injetando o <base href> do ambiente.
    Sem cache: garante que os usuários sempre recebam a versão atual da página."""
    with open(os.path.join(STATIC, name), encoding="utf-8") as f:
        html = f.read().replace("__BASE_HREF__", BASE_HREF)
    resp = Response(html, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp


def reader():
    global _reader
    if _reader is None:
        _reader = PdfReader(SOURCE_PDF)
    return _reader


_cover = None
CAPA = os.path.join(STATIC, "capa.jpg")


def cover_page():
    """Capa única do orçamento (Galeria Waldir Junior), preenchendo a página."""
    global _cover
    if _cover is not None:
        return _cover
    from reportlab.lib.utils import ImageReader
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(PAGE_W, PAGE_H))
    c.drawImage(ImageReader(CAPA), 0, 0, PAGE_W, PAGE_H)
    c.save()
    buf.seek(0)
    _cover = PdfReader(buf).pages[0]
    return _cover


def closing_page():
    """Página de encerramento com o bloco inferior esquerdo (©/Luminárias WJ)
    substituído por apenas 'Todos os direitos reservados.'."""
    global _closing
    if _closing is not None:
        return _closing
    src = reader().pages[CATALOG["closingPage"] - 1]
    pw, ph = float(src.mediabox.width), float(src.mediabox.height)
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(pw, ph))
    c.setFillColor(HexColor("#000000"))          # cobre o texto antigo (fundo é preto puro)
    c.rect(72, ph - 727, 250, 30, fill=1, stroke=0)
    c.setFillColor(HexColor("#b3b3b3"))          # cinza igual ao texto original
    c.setFont("Inter-Regular", 8.7)
    c.drawString(81, ph - 712, "Todos os direitos reservados.")
    c.save()
    buf.seek(0)
    src.merge_page(PdfReader(buf).pages[0])
    _closing = src
    return _closing


def brl(v):
    s = f"{v:,.2f}".replace(",", "\x00").replace(".", ",").replace("\x00", ".")
    return f"R$ {s}"


def spaced(text):
    return " ".join(text)  # tipografia espaçada, como no catálogo


def _wrap_text(text, font, size, max_w):
    """Quebra o texto em linhas que cabem em max_w (respeita quebras já existentes)."""
    out = []
    for para in (str(text).splitlines() or [""]):
        words = para.split()
        if not words:
            out.append("")
            continue
        cur = ""
        for wd in words:
            t = (cur + " " + wd).strip()
            if pdfmetrics.stringWidth(t, font, size) <= max_w:
                cur = t
            else:
                if cur:
                    out.append(cur)
                cur = wd
        if cur:
            out.append(cur)
    return out

def _paginate_rows(items, font, size, max_w, line_h, base_h, budget):
    """Wraps each item's name and splits items into pages by height (not by count)."""
    chunks, cur, used = [], [], 0
    for it in items:
        it["_lines"] = _wrap_text(it["name"], font, size, max_w) or [""]
        h = base_h + line_h * (len(it["_lines"]) - 1)
        if cur and used + h > budget:
            chunks.append(cur)
            cur, used = [], 0
        cur.append(it)
        used += h
    if cur:
        chunks.append(cur)
    return chunks or [[]]


VALIDITY_DAYS = 7  # validade fixa de todo orçamento


def build_budget_pages(client, items, total, observacao=""):
    """Página(s) de orçamento no estilo do catálogo (fundo preto)."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(PAGE_W, PAGE_H))
    white = HexColor("#f2f0eb")
    gray = HexColor("#9a968e")

    rows_per_page = 14
    chunks = [items[i:i + rows_per_page] for i in range(0, len(items), rows_per_page)] or [[]]

    for ci, chunk in enumerate(chunks):
        c.setFillColor(HexColor("#000000"))  # preto puro, igual à capa/encerramento
        c.rect(0, 0, PAGE_W, PAGE_H, fill=1, stroke=0)

        margin = 110
        y = PAGE_H - 95
        c.setFillColor(white)
        c.setFont("Inter-Semibold", 36)
        c.drawString(margin, y, spaced("ORÇAMENTO"))
        c.setFont("Inter-Regular", 16)
        c.setFillColor(gray)
        info = f"Data: {date.today().strftime('%d/%m/%Y')}"
        if client:
            info = f"Cliente: {client}    |    " + info
        if len(chunks) > 1:
            info += f"    |    Página {ci + 1} de {len(chunks)}"
        c.drawString(margin, y - 34, info)
        c.setStrokeColor(gray)
        c.setLineWidth(0.7)
        c.line(margin, y - 52, PAGE_W - margin, y - 52)

        # cabeçalho da tabela
        col_item = margin
        col_qty = PAGE_W - margin - 470
        col_unit = PAGE_W - margin - 330
        col_sub = PAGE_W - margin
        ty = y - 86
        c.setFont("Inter-Semibold", 14)
        c.setFillColor(gray)
        c.drawString(col_item, ty, "PRODUTO")
        c.drawRightString(col_qty + 60, ty, "QTD.")
        c.drawRightString(col_unit + 110, ty, "VALOR UNIT.")
        c.drawRightString(col_sub, ty, "SUBTOTAL")
        ty -= 14
        c.line(margin, ty, PAGE_W - margin, ty)

        c.setFillColor(white)
        ty -= 30
        for it in chunk:
            c.setFont("Inter-Regular", 17)
            c.drawString(col_item, ty, it["name"])
            c.drawRightString(col_qty + 60, ty, str(it["qty"]))
            c.drawRightString(col_unit + 110, ty, brl(it["price"]))
            c.drawRightString(col_sub, ty, brl(it["price"] * it["qty"]))
            ty -= 31

        # total somente na última página
        if ci == len(chunks) - 1:
            ty -= 8
            c.setStrokeColor(white)
            c.setLineWidth(1)
            c.line(col_unit - 160, ty + 18, PAGE_W - margin, ty + 18)
            c.setFont("Inter-Bold", 24)
            c.drawString(col_unit - 160, ty - 14, "TOTAL")
            c.drawRightString(col_sub, ty - 14, brl(total))
            if observacao:
                oy = ty - 62
                c.setFillColor(gray)
                c.setFont("Inter-Semibold", 12)
                c.drawString(margin, oy, spaced("OBSERVAÇÕES"))
                oy -= 24
                c.setFont("Inter-Regular", 14)
                c.setFillColor(white)
                for ln in _wrap_text(observacao, "Inter-Regular", 14, PAGE_W - 2 * margin):
                    if oy < 78:
                        break
                    c.drawString(margin, oy, ln)
                    oy -= 20

        c.setFont("Inter-Regular", 11)
        c.setFillColor(gray)
        c.drawString(margin, 48, f"Valores válidos por {VALIDITY_DAYS} dias. Orçamento sujeito a confirmação de disponibilidade.")
        c.showPage()

    c.save()
    buf.seek(0)
    return PdfReader(buf).pages


def build_budget_print(client, items, total, observacao=""):
    """Orçamento avulso para impressão: A4 vertical, fundo branco."""
    from reportlab.lib.pagesizes import A4
    pw, ph = A4
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    ink = HexColor("#1a1a1a")
    gray = HexColor("#6b675f")
    line = HexColor("#c9c5bd")

    margin = 54
    col_item = margin
    col_qty = pw - margin - 190      # a bit more room for the name
    col_unit = pw - margin - 100
    col_sub = pw - margin
    name_w = col_qty - col_item - 30  # max width for the product name

    chunks = _paginate_rows(items, "Inter-Regular", 11.5, name_w,
                            line_h=13, base_h=26, budget=480)

    for ci, chunk in enumerate(chunks):
        y = ph - 64
        c.setFillColor(ink)
        c.setFont("Inter-Bold", 15)
        c.drawString(margin, y, spaced("GALERIA WALDIR JUNIOR"))
        c.setFont("Inter-Semibold", 26)
        c.drawString(margin, y - 36, spaced("ORÇAMENTO"))
        c.setFillColor(gray)
        c.setFont("Inter-Regular", 11)
        info = f"Data: {date.today().strftime('%d/%m/%Y')}"
        if client:
            info = f"Cliente: {client}    |    " + info
        if len(chunks) > 1:
            info += f"    |    Página {ci + 1} de {len(chunks)}"
        c.drawString(margin, y - 58, info)
        c.setStrokeColor(line)
        c.setLineWidth(0.8)
        c.line(margin, y - 72, pw - margin, y - 72)

        
        ty = y - 96
        c.setFont("Inter-Semibold", 9.5)
        c.setFillColor(gray)
        c.drawString(col_item, ty, "PRODUTO")
        c.drawRightString(col_qty, ty, "QTD.")
        c.drawRightString(col_unit, ty, "VALOR UNIT.")
        c.drawRightString(col_sub, ty, "SUBTOTAL")
        ty -= 10
        c.line(margin, ty, pw - margin, ty)

        c.setFillColor(ink)
        ty -= 22
        for it in chunk:
            c.setFont("Inter-Regular", 11.5)
            for i, ln in enumerate(it["_lines"]):
                c.drawString(col_item, ty - i * 13, ln)
            c.drawRightString(col_qty, ty, str(it["qty"]))
            c.drawRightString(col_unit, ty, brl(it["price"]))
            c.drawRightString(col_sub, ty, brl(it["price"] * it["qty"]))
            ty -= 12 + (len(it["_lines"]) - 1) * 13
            c.setStrokeColor(line)
            c.setLineWidth(0.4)
            c.line(margin, ty, pw - margin, ty)
            ty -= 14

        if ci == len(chunks) - 1:
            ty -= 6
            c.setStrokeColor(ink)
            c.setLineWidth(1)
            c.line(col_qty - 80, ty + 14, pw - margin, ty + 14)
            c.setFillColor(ink)
            c.setFont("Inter-Bold", 15)
            c.drawString(col_qty - 80, ty - 8, "TOTAL")
            c.drawRightString(col_sub, ty - 8, brl(total))
            if observacao:
                oy = ty - 40
                c.setFillColor(gray)
                c.setFont("Inter-Semibold", 9.5)
                c.drawString(margin, oy, "OBSERVAÇÕES")
                oy -= 16
                c.setFont("Inter-Regular", 10.5)
                c.setFillColor(ink)
                for ln in _wrap_text(observacao, "Inter-Regular", 10.5, pw - 2 * margin):
                    if oy < 62:
                        break
                    c.drawString(margin, oy, ln)
                    oy -= 14

        c.setFillColor(gray)
        c.setFont("Inter-Regular", 8.5)
        c.drawString(margin, 40, f"Valores válidos por {VALIDITY_DAYS} dias. Orçamento sujeito a confirmação de disponibilidade.")
        c.showPage()

    c.save()
    buf.seek(0)
    return PdfReader(buf).pages


# ----------------------- vendedores e planilha de leads -----------------------
LEAD_COLUMNS = ["numero", "data_hora", "data", "mes", "vendedor", "cliente", "documento",
                "arquiteto", "whatsapp", "cidade", "tipo", "itens", "valor_total", "observacao"]
# uma linha por PRODUTO por orçamento — ligada ao leads.csv pela coluna "numero"
ITENS_COLUMNS = ["numero", "data", "vendedor", "produto", "marca", "cor",
                 "quantidade", "preco_unitario", "subtotal"]
_leads_lock = threading.Lock()


def _next_numero():
    """Migra o leads.csv para ter a coluna 'numero' (se ainda não tiver, renumerando
    as linhas existentes na ordem) e devolve o próximo número sequencial."""
    if not os.path.exists(LEADS_FILE):
        return 1
    with open(LEADS_FILE, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f, delimiter=";"))
    if not rows:
        return 1
    if "numero" not in rows[0] or not str(rows[0].get("numero") or "").strip():
        # planilha antiga sem a coluna: numera 1..N na ordem e reescreve
        for i, r in enumerate(rows, 1):
            r["numero"] = i
        with open(LEADS_FILE, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=LEAD_COLUMNS, delimiter=";", extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in LEAD_COLUMNS})
        return len(rows) + 1
    nums = [int(r["numero"]) for r in rows if str(r.get("numero", "")).strip().isdigit()]
    return (max(nums) + 1) if nums else len(rows) + 1


def _slug(s):
    """Gera um identificador simples (sem acento/espaço) para o usuário de login."""
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn").lower()
    return re.sub(r"[^a-z0-9]", "", s)


def _write_vendedores(lista):
    with open(VENDEDORES_FILE, "w", encoding="utf-8") as f:
        json.dump({"vendedores": lista}, f, ensure_ascii=False, indent=1)


def load_vendedores_raw():
    """Contas de vendedor: [{nome, usuario, senha_hash, ativo}].
    Migra automaticamente o formato antigo (lista de nomes) para contas."""
    if not os.path.exists(VENDEDORES_FILE):
        return []
    try:
        with open(VENDEDORES_FILE, encoding="utf-8") as f:
            lista = json.load(f).get("vendedores", [])
    except Exception:
        return []
    out, usuarios, changed = [], set(), False
    for v in lista:
        if isinstance(v, str):                       # formato antigo: só o nome
            nome = v.strip()
            if not nome:
                continue
            base = _slug(nome.split()[0]) or _slug(nome) or "vend"
            usuario, i = base, 2
            while usuario in usuarios:
                usuario = f"{base}{i}"
                i += 1
            out.append({"nome": nome, "usuario": usuario, "senha_hash": "",
                        "ativo": True, "cod_sapiens": ""})
            usuarios.add(usuario)
            changed = True
        else:
            v.setdefault("nome", "")
            v.setdefault("usuario", _slug(v.get("nome", "")))
            v.setdefault("senha_hash", "")
            v.setdefault("ativo", True)
            v.setdefault("cod_sapiens", "")
            out.append(v)
            usuarios.add(v["usuario"].lower())
    if changed:
        _write_vendedores(out)
    return out


def find_vendedor(usuario):
    usuario = (usuario or "").strip().lower()
    for v in load_vendedores_raw():
        if v["usuario"].lower() == usuario:
            return v
    return None


def vendedor_nome(usuario):
    v = find_vendedor(usuario)
    return v["nome"] if v else (usuario or "")


def append_lead(seller, client, architect, whatsapp, cidade, mode, items, total,
                observacao="", documento=""):
    """Registra o orçamento: uma linha-resumo em leads.csv e uma linha por
    produto em itens.csv (ligadas pela coluna 'numero')."""
    now = datetime.now()
    vend = seller or "Sem vendedor"
    with _leads_lock:                       # garante número único mesmo com gerações simultâneas
        numero = _next_numero()
        # resumo (leads.csv)
        novo = not os.path.exists(LEADS_FILE)
        row = {
            "numero": numero,
            "data_hora": now.strftime("%Y-%m-%d %H:%M:%S"),
            "data": now.strftime("%d/%m/%Y"),
            "mes": now.strftime("%Y-%m"),
            "vendedor": vend,
            "cliente": client or "",
            "documento": documento or "",
            "arquiteto": architect or "",
            "whatsapp": whatsapp or "",
            "cidade": cidade or "",
            "tipo": "Apresentação completa" if mode != "budget" else "Apenas orçamento",
            "itens": len(items),
            "valor_total": f"{total:.2f}",
            "observacao": (observacao or "").replace("\n", " ").strip(),
        }
        with open(LEADS_FILE, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=LEAD_COLUMNS, delimiter=";")
            if novo:
                w.writeheader()
            w.writerow(row)

        # itens do orçamento (itens.csv) — 1 linha por produto
        novo_i = not os.path.exists(ITENS_FILE)
        with open(ITENS_FILE, "a", newline="", encoding="utf-8") as f:
            wi = csv.DictWriter(f, fieldnames=ITENS_COLUMNS, delimiter=";")
            if novo_i:
                wi.writeheader()
            for it in items:
                p = it["product"]
                wi.writerow({
                    "numero": numero,
                    "data": now.strftime("%d/%m/%Y"),
                    "vendedor": vend,
                    "produto": p["name"],
                    "marca": p.get("brand", ""),
                    "cor": it.get("color", ""),
                    "quantidade": it["qty"],
                    "preco_unitario": f"{it['price']:.2f}",
                    "subtotal": f"{it['price'] * it['qty']:.2f}",
                })
    return numero


# ------------------------- orçamentos salvos (editáveis) ----------------------
# Cada orçamento é um arquivo data/orcamentos/<numero>.json com dono (vendedor),
# status (aberto/ganho/perdido) e histórico de versões (preços congelados por versão).
ORC_COLUMNS = ["numero", "criado_em", "atualizado_em", "ganho_em", "vendedor", "cliente",
               "documento", "cod_cli_senior", "arquiteto", "whatsapp", "cidade", "tipo", "status", "versao_atual",
               "itens", "valor_total", "valor_wj", "valor_bella", "observacao"]
STATUS_VALIDOS = {"aberto", "ganho", "perdido"}
_orc_lock = threading.Lock()


def orc_path(numero):
    return os.path.join(ORC_DIR, f"{int(numero)}.json")


def load_orcamento(numero):
    p = orc_path(numero)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _versao_atual(obj):
    return obj["versoes"][-1] if obj.get("versoes") else {"itens": [], "total": 0}


def _split_marca(versao):
    """Soma o valor de uma versão separando por marca -> (wj, bella)."""
    wj = bella = 0.0
    for it in versao.get("itens", []):
        sub = float(it.get("preco", 0)) * int(it.get("qtd", 0))
        if "bella" in (it.get("marca", "") or "").lower():
            bella += sub
        else:
            wj += sub
    return round(wj, 2), round(bella, 2)


def _versao_from_items(items, v):
    """Monta uma versão a partir da lista de itens (congela os preços do momento)."""
    return {
        "v": v,
        "criado_em": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "itens": [{
            "id": it["product"]["id"] if it.get("product") else it.get("id"),
            "produto": (it["product"]["name"] if it.get("product") else it.get("produto", "")),
            "marca": (it["product"].get("brand", "") if it.get("product") else it.get("marca", "")),
            "cor": it.get("color", it.get("cor", "")),
            "qtd": it["qty"],
            "preco": it["price"],
        } for it in items],
        "total": sum(it["price"] * it["qty"] for it in items),
    }


def _pf_envios(pf):
    """Retorna o histórico de envios à fábrica.

    Formato atual:
        pedido_fabrica = {"envios": [...]}

    Formato legado:
        pedido_fabrica era ele próprio um único envio.
    """
    if not pf:
        return []
    envios = pf.get("envios")
    if isinstance(envios, list):
        return envios
    return [pf]


def _pf_para_historico(pf):
    """Converte o pedido_fabrica legado para o container de histórico."""
    if not pf:
        return {"envios": []}
    if isinstance(pf.get("envios"), list):
        return pf
    return {"envios": [pf]}


def _ultimo_envio(pf):
    envios = _pf_envios(pf)
    return envios[-1] if envios else None


def _marca_id(marca):
    return "BELLA" if "bella" in (marca or "").lower() else "WJ"


def _external_order_id(numero, marca):
    return f"ORC-{int(numero)}-{_marca_id(marca)}"


def _latest_senior_number_for_external_id(pf, external_order_id):
    """Último numPed confirmado para o pedido lógico/marca."""
    for envio in reversed(_pf_envios(pf)):
        for sub in reversed(envio.get("pedidos") or []):
            sub_external = sub.get("external_order_id")
            if not sub_external:
                sub_external = _external_order_id(
                    sub.get("payload", {}).get("numeroOrcamento") or 0,
                    sub.get("marca", ""),
                )
            if (
                sub_external == external_order_id
                and sub.get("status") == "enviado"
                and sub.get("numero_pedido")
                and not str(sub.get("numero_pedido")).upper().startswith("MOCK-")
            ):
                return str(sub.get("numero_pedido"))
    return None


def _confirmed_brand_ids(pf):
    """Marcas que já possuem um pedido Senior confirmado em qualquer envio anterior."""
    out = set()
    for envio in _pf_envios(pf):
        for sub in envio.get("pedidos") or []:
            numero_pedido = str(sub.get("numero_pedido") or "")
            if (
                sub.get("status") == "enviado"
                and numero_pedido
                and not numero_pedido.upper().startswith("MOCK-")
            ):
                out.add(_marca_id(sub.get("marca", "")))
    return out

def _transacoes_historicas(pf):
    """Retorna última transação por marca e transações bloqueadas."""
    recentes, fixas = {}, {}

    for envio in _pf_envios(pf):
        for sub in envio.get("pedidos") or []:
            marca = _marca_id(sub.get("marca", ""))
            codigo = str(
                (sub.get("payload") or {}).get("tnspro") or ""
            ).strip()

            if not codigo:
                continue

            recentes[marca] = codigo

            numero_pedido = str(sub.get("numero_pedido") or "")

            confirmado = (
                sub.get("status") == "enviado"
                and numero_pedido
                and not numero_pedido.upper().startswith("MOCK-")
            )

            em_andamento = sub.get("status") in {
                "recebido_agente",
                "processando_senior"
            }

            if confirmado or em_andamento:
                fixas.setdefault(marca, codigo)

    return recentes, fixas

def _empresas_mantidas_no_senior(pf, pedidos, numero):
    """Empresas sem itens na versão atual, mas com pedido Senior anterior.

    Não gera jobs, não cancela pedidos e não altera o histórico.
    """
    marcas_atuais = {
        _marca_id(p.get("marca", ""))
        for p in pedidos
    }

    marcas_confirmadas = _confirmed_brand_ids(pf)

    mantidas = []

    for marca in sorted(marcas_confirmadas - marcas_atuais):
        external_id = f"ORC-{numero}-{marca}"

        mantidas.append({
            "marca": marca,
            "external_order_id": external_id,
            "numero_pedido": _latest_senior_number_for_external_id(
                pf, external_id
            ),
            "situacao": "mantido_sem_alteracao",
        })

    return mantidas


def _envio_status(envio):
    """Resume um envio específico (que pode ter WJ, Bella ou ambos)."""
    if not envio:
        return {"status": "", "numeros": [], "n": 0}

    subs = envio.get("pedidos") or []
    if not subs:  # compatibilidade com formato antigo de payload único
        numero = envio.get("numero_pedido")
        return {
            "status": envio.get("status", ""),
            "numeros": [numero] if numero else [],
            "n": 1,
        }

    estados = [s.get("status", "pendente") for s in subs]
    numeros = [s.get("numero_pedido") for s in subs if s.get("numero_pedido")]

    # Jobs substituídos deixam de participar do status efetivo daquele envio.
    ativos = [e for e in estados if e != "substituido"]

    if not ativos:
        geral = "substituido"
    elif any(e == "erro" for e in ativos):
        # Em pedido misto, um sucesso + uma falha é um envio parcial, não uma
        # falha total do orçamento. A UI mostra o detalhe de cada empresa.
        geral = "parcial" if any(e == "enviado" for e in ativos) else "erro"
    elif all(e == "enviado" for e in ativos):
        geral = "enviado"
    elif any(e == "pendente" for e in ativos):
        geral = "pendente"
    elif all(e in ("recebido_agente", "processando_senior", "enviado") for e in ativos):
        geral = "recebido_agente"
    elif all(e in ("enviado", "nao_enviar") for e in ativos):
        # Quando o usuário encerra deliberadamente uma empresa com "não reenviar",
        # o envio deixa de estar pendente/erro. Se outra empresa foi enviada, este
        # é um resultado terminal concluído parcialmente.
        if any(e == "enviado" for e in ativos) and any(e == "nao_enviar" for e in ativos):
            geral = "concluido_parcial"
        elif all(e == "nao_enviar" for e in ativos):
            geral = "nao_enviar"
        else:
            geral = "enviado"
    else:
        geral = "pendente"

    return {"status": geral, "numeros": numeros, "n": len(subs)}


def _pf_status(pf):
    """Resume o envio MAIS RECENTE do histórico de fábrica."""
    envios = _pf_envios(pf)
    if not envios:
        return {
            "enviado": False,
            "status": "",
            "numeros": [],
            "n": 0,
            "versao": None,
            "n_envios": 0,
        }

    ultimo = envios[-1]
    st = _envio_status(ultimo)
    return {
        "enviado": True,
        "status": st["status"],
        "numeros": st["numeros"],
        "n": st["n"],
        "versao": ultimo.get("versao"),
        "n_envios": len(envios),
    }


def _find_relay_job(pf, job_id=None, indice=None):
    """Localiza um job de relay dentro de todo o histórico.

    Para o formato novo, job_id é a identidade principal.
    numero+indice fica apenas como compatibilidade com um único envio legado.
    """
    envios = _pf_envios(pf)

    if job_id:
        for envio in envios:
            for i, sub in enumerate(envio.get("pedidos") or []):
                if sub.get("job_id") == job_id:
                    return envio, sub, i
        return None, None, None

    if len(envios) != 1:
        return None, None, None

    envio = envios[0]
    subs = envio.get("pedidos") or []

    if not subs:
        return envio, envio, None

    if indice is None:
        return None, None, None

    try:
        indice = int(indice)
    except (TypeError, ValueError):
        return None, None, None

    if indice < 0 or indice >= len(subs):
        return None, None, None

    return envio, subs[indice], indice


def _has_newer_brand_submission(pf, envio_alvo, sub_alvo):
    """True se a mesma empresa/pedido lógico já possui envio de versão posterior."""
    versao_alvo = (envio_alvo or {}).get("versao")
    external_order_id = (sub_alvo or {}).get("external_order_id")

    if versao_alvo is None or not external_order_id:
        return False

    for envio in _pf_envios(pf):
        versao = envio.get("versao")
        if versao is None or versao <= versao_alvo:
            continue
        for sub in envio.get("pedidos") or []:
            if sub.get("external_order_id") == external_order_id:
                return True
    return False


def _registrar_retry_history(sub, *, acao, agora, por):
    hist = sub.setdefault("historico_reprocessamento", [])
    hist.append({
        "acao": acao,
        "em": agora,
        "por": por,
        "status_anterior": sub.get("status"),
        "erro_anterior": sub.get("erro"),
        "processado_em_anterior": sub.get("processado_em"),
    })


def orc_resumo(obj):
    v = _versao_atual(obj)
    pf = _pf_status(obj.get("pedido_fabrica"))
    versao_atual = obj.get("versao_atual", len(obj.get("versoes", [])))
    versao_enviada = pf.get("versao")

    # Se a versão atual é posterior ao último envio, há alterações ainda não enviadas.
    pedido_desatualizado = bool(
        versao_enviada is not None and versao_atual > versao_enviada
    )

    # Uma mesma versão só pode gerar um envio. Nova versão = novo job.
    versoes_enviadas = {
        envio.get("versao")
        for envio in _pf_envios(obj.get("pedido_fabrica"))
        if envio.get("versao") is not None
    }
    pode_enviar = versao_atual not in versoes_enviadas

    return {
        "numero": obj["numero"],
        "vendedor": obj.get("vendedor", ""),
        "vendedor_nome": vendedor_nome(obj.get("vendedor", "")),
        "cliente": obj.get("cliente", ""),
        "documento": obj.get("documento", ""),
        "cod_cli_senior": obj.get("cod_cli_senior", ""),
        "status": obj.get("status", "aberto"),
        "tipo": obj.get("tipo", "budget"),
        "versao_atual": versao_atual,
        "n_versoes": len(obj.get("versoes", [])),
        "n_itens": len(v.get("itens", [])),
        "valor_atual": round(v.get("total", 0), 2),
        "observacao": obj.get("observacao", ""),
        "criado_em": obj.get("criado_em", ""),
        "atualizado_em": obj.get("atualizado_em", ""),
        "ganho_em": obj.get("ganho_em", ""),
        "enviado_fabrica": pf["enviado"],
        "pedido_status": pf["status"],
        "pedido_numero": (pf["numeros"][0] if pf["numeros"] else None),
        "pedido_numeros": pf["numeros"],
        "pedido_n": pf["n"],
        "pedido_versao": versao_enviada,
        "pedido_n_envios": pf.get("n_envios", 0),
        "pedido_desatualizado": pedido_desatualizado,
        "pode_enviar_fabrica": pode_enviar,
    }


def list_orcamentos(owner=None):
    """Resumo de todos os orçamentos salvos (ou só os do vendedor 'owner')."""
    out = []
    for fn in os.listdir(ORC_DIR):
        if not fn.endswith(".json"):
            continue
        obj = load_orcamento(fn[:-5])
        if not obj:
            continue
        if owner and obj.get("vendedor") != owner:
            continue
        out.append(orc_resumo(obj))
    out.sort(key=lambda x: x["numero"], reverse=True)
    return out


def write_orcamentos_csv():
    """(Re)escreve o orcamentos.csv derivado do store — 1 linha por orçamento,
    com o status e o valor da versão atual (para o Power BI medir conversão)."""
    rows = []
    for fn in os.listdir(ORC_DIR):
        if not fn.endswith(".json"):
            continue
        obj = load_orcamento(fn[:-5])
        if not obj:
            continue
        v = _versao_atual(obj)
        wj, bella = _split_marca(v)
        rows.append({
            "numero": obj["numero"],
            "criado_em": obj.get("criado_em", ""),
            "atualizado_em": obj.get("atualizado_em", ""),
            "ganho_em": obj.get("ganho_em", ""),
            "vendedor": vendedor_nome(obj.get("vendedor", "")),
            "cliente": obj.get("cliente", ""),
            "documento": obj.get("documento", ""),
            "cod_cli_senior": obj.get("cod_cli_senior", ""),
            "arquiteto": obj.get("arquiteto", ""),
            "whatsapp": obj.get("whatsapp", ""),
            "cidade": obj.get("cidade", ""),
            "tipo": "Apresentação completa" if obj.get("tipo") != "budget" else "Apenas orçamento",
            "status": obj.get("status", "aberto"),
            "versao_atual": obj.get("versao_atual", 1),
            "itens": len(v.get("itens", [])),
            "valor_total": f'{v.get("total", 0):.2f}',
            "valor_wj": f"{wj:.2f}",
            "valor_bella": f"{bella:.2f}",
            "observacao": (obj.get("observacao", "") or "").replace("\n", " ").strip(),
        })
    rows.sort(key=lambda r: r["numero"])
    with open(ORC_CSV_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=ORC_COLUMNS, delimiter=";")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def save_orcamento(obj):
    """Grava o JSON de forma atômica para o relay nunca ler arquivo pela metade."""
    destino = orc_path(obj["numero"])
    temporario = f"{destino}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(temporario, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporario, destino)
    finally:
        if os.path.exists(temporario):
            try:
                os.remove(temporario)
            except OSError:
                pass
    write_orcamentos_csv()


def create_orcamento(numero, owner, client, architect, whatsapp, cidade, mode, items,
                     observacao="", documento="", cod_cli_senior=""):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    obj = {
        "numero": numero,
        "vendedor": owner or "",
        "cliente": client or "", "arquiteto": architect or "",
        "whatsapp": whatsapp or "", "cidade": cidade or "",
        "documento": (documento or "").strip(),
        "cod_cli_senior": str(cod_cli_senior or "").strip(),
        "observacao": (observacao or "").strip(),
        "tipo": "budget" if mode == "budget" else "full",
        "status": "aberto", "ganho_em": "", "criado_em": now, "atualizado_em": now,
        "versao_atual": 1, "versoes": [_versao_from_items(items, 1)],
    }
    with _orc_lock:
        save_orcamento(obj)
    return obj


def _build_items(raw_items):
    """Resolve os itens do carrinho contra o catálogo ATUAL (preço vigente)."""
    items = []
    for it in raw_items:
        p = PRODUCTS.get(int(it.get("id", 0)))
        if not p:
            continue
        color = (it.get("color") or "").strip()
        items.append({
            "product": p,
            "name": p["name"] + (f" — {color}" if color else ""),
            "color": color,
            "qty": max(1, int(it.get("qty") or 1)),
            "price": float(p.get("price") or 0),  # preço fixo do catálogo
        })
    items.sort(key=lambda x: x["product"]["pages"][0])
    return items


def build_pdf(client, items, total, mode, observacao=""):
    """Monta o PDF do orçamento (apresentação completa ou só orçamento A4)."""
    w = PdfWriter()
    if mode == "budget":
        for page in build_budget_print(client, items, total, observacao):
            w.add_page(page)
    else:
        r = reader()
        w.add_page(cover_page())
        for it in items:
            if it.get("product"):
                for pg in it["product"]["pages"]:
                    w.add_page(r.pages[pg - 1])
        for page in build_budget_pages(client, items, total, observacao):
            w.add_page(page)
        w.add_page(closing_page())
    out = io.BytesIO()
    w.write(out)
    out.seek(0)
    return out


def _ensure_csv_columns(path, columns):
    """Se a planilha existir com um cabeçalho antigo (colunas novas faltando), reescreve
    com o cabeçalho atual preenchendo as células que faltarem — preserva os dados.
    Torna seguro adicionar colunas (ex.: 'observacao') numa planilha já existente."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8", newline="") as f:
        try:
            header = next(csv.reader(f, delimiter=";"))
        except StopIteration:
            return
    if header == columns:
        return
    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f, delimiter=";"))
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns, delimiter=";", extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in columns})


# migra a planilha existente (numera as linhas antigas + adiciona colunas novas) na subida
try:
    _next_numero()
    _ensure_csv_columns(LEADS_FILE, LEAD_COLUMNS)
except Exception:
    pass


def read_leads():
    if not os.path.exists(LEADS_FILE):
        return []
    with open(LEADS_FILE, encoding="utf-8") as f:
        return list(csv.DictReader(f, delimiter=";"))


def _remove_numero_from_csv(path, columns, numero):
    """Remove todas as linhas de um determinado 'numero' de uma planilha (leads/itens)."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f, delimiter=";"))
    kept = [r for r in rows if str(r.get("numero", "")) != str(numero)]
    if len(kept) == len(rows):
        return
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns, delimiter=";", extrasaction="ignore")
        w.writeheader()
        for r in kept:
            w.writerow({k: r.get(k, "") for k in columns})


@app.route("/api/vendedores")
def api_vendedores():
    """Nomes ativos (compat. com o relatório) + contas para o painel admin."""
    contas = [{"nome": v["nome"], "usuario": v["usuario"], "ativo": v.get("ativo", True),
               "tem_senha": bool(v.get("senha_hash")), "cod_sapiens": v.get("cod_sapiens", "")}
              for v in load_vendedores_raw()]
    contas.sort(key=lambda c: c["nome"].lower())
    nomes = [c["nome"] for c in contas if c["ativo"]]
    return jsonify({"vendedores": nomes, "contas": contas})


@app.route("/api/vendedores", methods=["POST"])
@require_admin
def api_vendedores_add():
    d = request.get_json(force=True) or {}
    nome = (d.get("nome") or "").strip()
    usuario = (d.get("usuario") or "").strip().lower() or _slug((nome.split() or [""])[0])
    senha = d.get("senha") or ""
    if not nome:
        return jsonify({"error": "Informe o nome do vendedor."}), 400
    if not usuario:
        return jsonify({"error": "Informe o usuário de login."}), 400
    if len(senha) < 4:
        return jsonify({"error": "A senha deve ter ao menos 4 caracteres."}), 400
    lista = load_vendedores_raw()
    if any(v["usuario"].lower() == usuario for v in lista):
        return jsonify({"error": "Já existe um vendedor com esse usuário."}), 400
    lista.append({"nome": nome, "usuario": usuario,
                  "senha_hash": generate_password_hash(senha), "ativo": True,
                  "cod_sapiens": str(d.get("cod_sapiens") or "").strip()})
    _write_vendedores(lista)
    return jsonify({"ok": True})


@app.route("/api/vendedores/sapiens", methods=["POST"])
@require_admin
def api_vendedores_sapiens():
    """Define o código do vendedor no Sapiens, atrelado à conta de login."""
    d = request.get_json(force=True) or {}
    usuario = (d.get("usuario") or "").strip().lower()
    cod = str(d.get("cod_sapiens") or "").strip()
    lista = load_vendedores_raw()
    for v in lista:
        if v["usuario"].lower() == usuario:
            v["cod_sapiens"] = cod
            _write_vendedores(lista)
            return jsonify({"ok": True})
    return jsonify({"error": "Vendedor não encontrado."}), 404


@app.route("/api/vendedores/senha", methods=["POST"])
@require_admin
def api_vendedores_senha():
    d = request.get_json(force=True) or {}
    usuario = (d.get("usuario") or "").strip().lower()
    senha = d.get("senha") or ""
    if len(senha) < 4:
        return jsonify({"error": "A senha deve ter ao menos 4 caracteres."}), 400
    lista = load_vendedores_raw()
    for v in lista:
        if v["usuario"].lower() == usuario:
            v["senha_hash"] = generate_password_hash(senha)
            _write_vendedores(lista)
            return jsonify({"ok": True})
    return jsonify({"error": "Vendedor não encontrado."}), 404


@app.route("/api/vendedores/ativo", methods=["POST"])
@require_admin
def api_vendedores_ativo():
    d = request.get_json(force=True) or {}
    usuario = (d.get("usuario") or "").strip().lower()
    ativo = bool(d.get("ativo"))
    lista = load_vendedores_raw()
    for v in lista:
        if v["usuario"].lower() == usuario:
            v["ativo"] = ativo
            _write_vendedores(lista)
            return jsonify({"ok": True})
    return jsonify({"error": "Vendedor não encontrado."}), 404


@app.route("/api/vendedores", methods=["DELETE"])
@require_admin
def api_vendedores_del():
    usuario = (request.get_json(force=True) or {}).get("usuario", "").strip().lower()
    lista = [v for v in load_vendedores_raw() if v["usuario"].lower() != usuario]
    _write_vendedores(lista)
    return jsonify({"ok": True})


# --------------------------- login do vendedor -------------------------------
@app.route("/api/vendedor/login", methods=["POST"])
def vendedor_login():
    d = request.get_json(force=True) or {}
    usuario = (d.get("usuario") or "").strip().lower()
    senha = d.get("senha") or ""
    v = find_vendedor(usuario)
    if not v or not v.get("ativo", True):
        return jsonify({"error": "Usuário não encontrado ou inativo."}), 401
    if not v.get("senha_hash") or not check_password_hash(v["senha_hash"], senha):
        return jsonify({"error": "Usuário ou senha incorretos."}), 401
    session["vendedor"] = v["usuario"]
    return jsonify({"usuario": v["usuario"], "nome": v["nome"]})


@app.route("/api/vendedor/logout", methods=["POST"])
def vendedor_logout():
    session.pop("vendedor", None)
    return jsonify({"ok": True})


@app.route("/api/vendedor/me")
def vendedor_me():
    v = current_vendedor()
    if v:
        return jsonify({"usuario": v["usuario"], "nome": v["nome"]})
    return jsonify({})


# --------------------------- orçamentos salvos -------------------------------
def _can_access(obj):
    v = current_vendedor()
    if v and obj.get("vendedor") == v["usuario"]:
        return True
    return admin_logged_in()


@app.route("/api/orcamentos")
def api_orcamentos():
    """Lista os orçamentos do vendedor logado; se for admin, lista todos."""
    v = current_vendedor()
    if v:
        return jsonify({"orcamentos": list_orcamentos(owner=v["usuario"]), "escopo": "meus"})
    if admin_logged_in():
        return jsonify({"orcamentos": list_orcamentos(), "escopo": "todos"})
    return jsonify({"error": "Faça login."}), 401


@app.route("/api/orcamento/<int:numero>")
def api_orcamento_get(numero):
    obj = load_orcamento(numero)
    if not obj:
        return jsonify({"error": "Orçamento não encontrado."}), 404
    if not _can_access(obj):
        return jsonify({"error": "Sem permissão."}), 403
    return jsonify(obj)


@app.route("/api/orcamento/<int:numero>/cliente-senior", methods=["POST"])
def api_orcamento_cliente_senior(numero):
    """Define/atualiza o código do cliente já cadastrado no Senior.

    Isso NÃO cria uma nova versão comercial do orçamento; é apenas um vínculo de
    integração necessário antes do envio à fábrica.
    """
    d = request.get_json(force=True) or {}
    bruto = str(d.get("cod_cli_senior") or "").strip()

    if not bruto.isdigit() or int(bruto) <= 0:
        return jsonify({"error": "Informe um código de cliente Senior válido (maior que zero)."}), 400

    with _orc_lock:
        obj = load_orcamento(numero)
        if not obj:
            return jsonify({"error": "Orçamento não encontrado."}), 404
        if not _can_access(obj):
            return jsonify({"error": "Sem permissão."}), 403

        obj["cod_cli_senior"] = str(int(bruto))
        obj["atualizado_em"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_orcamento(obj)

    return jsonify({"ok": True, "cod_cli_senior": obj["cod_cli_senior"]})


@app.route("/api/orcamento/<int:numero>/status", methods=["POST"])
def api_orcamento_status(numero):
    status = (request.get_json(force=True) or {}).get("status", "")
    if status not in STATUS_VALIDOS:
        return jsonify({"error": "Status inválido."}), 400

    with _orc_lock:
        obj = load_orcamento(numero)
        if not obj:
            return jsonify({"error": "Orçamento não encontrado."}), 404
        if not _can_access(obj):
            return jsonify({"error": "Sem permissão."}), 403

        agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        obj["status"] = status
        obj["atualizado_em"] = agora
        # registra QUANDO virou "ganho" (data da venda) para o relatório de vendas por mês
        if status == "ganho":
            obj["ganho_em"] = agora
        else:
            obj["ganho_em"] = ""
        save_orcamento(obj)

    return jsonify({"ok": True, "status": status})


@app.route("/api/orcamento/<int:numero>", methods=["DELETE"])
def api_orcamento_del(numero):
    """Exclui um orçamento salvo (e as linhas correspondentes em leads/itens)."""
    with _orc_lock:
        obj = load_orcamento(numero)
        if not obj:
            return jsonify({"error": "Orçamento não encontrado."}), 404
        if not _can_access(obj):
            return jsonify({"error": "Sem permissão."}), 403

        # Depois que existe um envio à fábrica, o JSON vira parte do histórico
        # de integração. Excluir quebraria ACKs/resultados ainda em trânsito.
        if _pf_envios(obj.get("pedido_fabrica")):
            return jsonify({
                "error": "Este orçamento já possui histórico de envio à fábrica e não pode ser excluído."
            }), 409

        p = orc_path(numero)
        if os.path.exists(p):
            os.remove(p)
        write_orcamentos_csv()

    with _leads_lock:
        _remove_numero_from_csv(LEADS_FILE, LEAD_COLUMNS, numero)
        _remove_numero_from_csv(ITENS_FILE, ITENS_COLUMNS, numero)
    return jsonify({"ok": True})


@app.route("/api/orcamento/<int:numero>/versao", methods=["POST"])
def api_orcamento_versao(numero):
    """Salva uma NOVA versão do orçamento com os preços ATUAIS do catálogo.
    A(s) versão(ões) anterior(es) permanecem congeladas."""
    d = request.get_json(force=True) or {}
    items = _build_items(d.get("items") or [])
    if not items:
        return jsonify({"error": "Nenhum produto válido."}), 400

    with _orc_lock:
        obj = load_orcamento(numero)
        if not obj:
            return jsonify({"error": "Orçamento não encontrado."}), 404
        if not _can_access(obj):
            return jsonify({"error": "Sem permissão."}), 403

        for k_json, k_obj in [("client", "cliente"), ("architect", "arquiteto"),
                              ("whatsapp", "whatsapp"), ("city", "cidade"),
                              ("observacao", "observacao"), ("documento", "documento"),
                              ("cod_cli_senior", "cod_cli_senior")]:
            if k_json in d:
                obj[k_obj] = (d.get(k_json) or "").strip()
        if d.get("mode") in ("budget", "full"):
            obj["tipo"] = d["mode"]

        nova_v = obj.get("versao_atual", len(obj.get("versoes", []))) + 1
        obj["versoes"].append(_versao_from_items(items, nova_v))
        obj["versao_atual"] = nova_v
        obj["atualizado_em"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_orcamento(obj)

    return jsonify({"ok": True, "versao": nova_v, "resumo": orc_resumo(obj)})


@app.route("/api/orcamento/<int:numero>/pdf")
def api_orcamento_pdf(numero):
    """Re-baixa o PDF de uma versão salva (preços congelados daquela versão)."""
    obj = load_orcamento(numero)
    if not obj:
        return jsonify({"error": "Orçamento não encontrado."}), 404
    if not _can_access(obj):
        return jsonify({"error": "Sem permissão."}), 403
    vsel = request.args.get("v")
    versao = next((vv for vv in obj["versoes"] if str(vv["v"]) == str(vsel)), None) \
        if vsel else None
    versao = versao or _versao_atual(obj)
    mode = request.args.get("mode") or obj.get("tipo", "budget")
    items = []
    for it in versao["itens"]:
        p = PRODUCTS.get(it.get("id"))
        cor = it.get("cor", "")
        items.append({
            "product": p,   # None se o produto saiu do catálogo (só afeta modo completo)
            "name": it["produto"] + (f" — {cor}" if cor else ""),
            "qty": it["qtd"],
            "price": it["preco"],
        })
    total = sum(i["price"] * i["qty"] for i in items)
    out = build_pdf(obj.get("cliente", ""), items, total, mode, obj.get("observacao", ""))
    safe_client = re.sub(r"[^\w \-]", "", obj.get("cliente", ""))[:60].strip()
    fname = f"Orçamento {obj['numero']} v{versao['v']}"
    if safe_client:
        fname += f" - {safe_client}"
    fname += ".pdf"
    return send_file(out, mimetype="application/pdf", as_attachment=True, download_name=fname)


# ------------------- integração Sapiens (pedido de venda à fábrica) -------------------
@app.route("/api/sapiens/config", methods=["GET", "POST"])
@require_admin
def api_sapiens_config():
    if request.method == "POST":
        cfg = sapiens.save_config(request.get_json(force=True) or {})
    else:
        cfg = sapiens.load_config()
    return jsonify({"config": cfg, "modo": sapiens.MODO})


@app.route("/api/sapiens/map", methods=["GET", "POST"])
@require_admin
def api_sapiens_map():
    """GET devolve o de-para atual + os nomes dos produtos do catálogo.
    POST grava o código Sapiens de um produto (código vazio remove o de-para)."""
    if request.method == "POST":
        d = request.get_json(force=True) or {}
        m = sapiens.set_produto(
            d.get("nome", ""),
            codigo=d.get("codigo") if "codigo" in d else None,
            unidade=d.get("unidade") if "unidade" in d else None,
            derivacoes=d.get("derivacoes") if "derivacoes" in d else None,
        )
        return jsonify({"ok": True, "map": m})
    return jsonify({
        "map": sapiens.load_map(),
        "verificados": senior_index.applied_mappings(),
        "produtos": [
            {
                "name": p["name"],
                "brand": p.get("brand", "")
            }
            for p in CATALOG["products"]
        ]
    })
    
    
# ---------------- Senior product directory and derivation lookup ----------------

@app.route("/api/sapiens/produtos-index/status")
@require_admin
def api_senior_index_status():
    return jsonify({"indices": senior_index.metadata()})


@app.route("/api/sapiens/produtos-search")
@require_admin
def api_senior_products_search():
    query = (request.args.get("q") or "").strip()[:120]
    brand = request.args.get("marca", "")
    environment = request.args.get("ambiente", "producao")

    try:
        result = senior_index.search(query, brand, environment)

        return jsonify({
            "produtos": result,
            "total_exibidos": len(result),
            "ambiente": environment,
            "marca": senior_index.canonical_brand(brand)
        })
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/sapiens/derivacao-jobs", methods=["POST"])
@require_admin
def api_senior_deriv_jobs_create():
    data = request.get_json(silent=True) or {}

    web_name = (data.get("nome") or "").strip()
    code = (data.get("codigo") or "").strip()
    brand = (data.get("marca") or "").strip()
    environment = (data.get("ambiente") or "producao").strip()

    web_product = next(
        (p for p in CATALOG["products"] if p["name"] == web_name),
        None
    )

    if not web_product:
        return jsonify({
            "error": "Produto não existe no catálogo atual."
        }), 404

    try:
        if (
            senior_index.canonical_brand(brand)
            != senior_index.canonical_brand(web_product.get("brand", ""))
        ):
            return jsonify({
                "error": "Marca não corresponde ao produto do site."
            }), 400

        if not code:
            return jsonify({
                "error": "Selecione um código Senior."
            }), 400

        job = senior_index.create_job(
            web_name,
            brand,
            code,
            environment
        )

        return jsonify({"ok": True, "job": job}), 202

    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/sapiens/derivacao-jobs", methods=["GET"])
@require_admin
def api_senior_deriv_jobs_list():
    # Compact status indicators beside each product.
    if request.args.get("resumo") == "1":
        return jsonify({
            "jobs": senior_index.latest_jobs_per_product()
        })

    return jsonify({
        "jobs": senior_index.recent_jobs()
    })


@app.route("/api/sapiens/derivacao-jobs/<job_id>")
@require_admin
def api_senior_deriv_job_status(job_id):
    job = senior_index.get_job(job_id)

    if not job:
        return jsonify({"error": "Job não encontrado."}), 404

    return jsonify({"job": job})


@app.route(
    "/api/sapiens/derivacao-jobs/<job_id>/aplicar",
    methods=["POST"]
)
@require_admin
def api_senior_deriv_job_apply(job_id):
    job = senior_index.get_job(job_id)

    if not job:
        return jsonify({"error": "Job não encontrado."}), 404

    if job["status"] != "completed":
        return jsonify({
            "error": "Aguarde a consulta finalizar com sucesso."
        }), 409

    expected_source = (
        "senior_homolog"
        if job["environment"] == "homolog"
        else "senior_producao"
    )

    if job["result_source"] != expected_source:
        return jsonify({
            "error": "Resposta simulada não pode ser gravada no cadastro."
        }), 409

    web_product = next(
        (
            p for p in CATALOG["products"]
            if p["name"] == job["web_product"]
        ),
        None
    )

    if (
        not web_product
        or senior_index.canonical_brand(
            web_product.get("brand", "")
        ) != job["brand"]
    ):
        return jsonify({
            "error": "Produto do catálogo foi removido ou mudou de marca."
        }), 409

    if not senior_index.find_product(
        job["code"],
        job["brand"],
        job["environment"]
    ):
        return jsonify({
            "error": "Código não consta mais no índice Senior."
        }), 409

    # Save Senior's real unit of measure, never an old manual value.
    # Older jobs without uniMed must be queried again.
    unit = (job.get("unidade") or "").strip()

    if not unit:
        return jsonify({
            "error": (
                "Consulta sem uniMed. Faça uma nova consulta após "
                "atualizar o PowerShell e o F000CWS."
            )
        }), 409

    # Only this explicit admin action saves the mapping.
    mapped = sapiens.set_produto(
        job["web_product"],
        codigo=job["code"],
        unidade=unit,
        derivacoes=job["derivations"]
    )

    senior_index.mark_applied(job_id)

    return jsonify({
        "ok": True,
        "map": mapped,
        "n": len(job["derivations"])
    })


# ---------------- Endpoints consumed manually by PowerShell ----------------

@app.route("/api/relay/derivacoes-pendentes")
def api_relay_deriv_jobs_pending():
    if not _relay_ok():
        return jsonify({
            "error": "Token do relay inválido."
        }), 401

    return jsonify({
        "pendentes": senior_index.pending_jobs()
    })


@app.route("/api/relay/derivacoes-resultado", methods=["POST"])
def api_relay_deriv_jobs_result():
    if not _relay_ok():
        return jsonify({
            "error": "Token do relay inválido."
        }), 401

    data = request.get_json(silent=True) or {}

    try:
        job = senior_index.complete_job(
            (data.get("job_id") or "").strip(),
            data.get("status", ""),
            derivations=data.get("derivacoes"),
            error=data.get("erro", ""),
            source=data.get("source", "mock"),
            unidade=data.get("unidade")
        )

        return jsonify({
            "ok": True,
            "job_id": job["job_id"],
            "status": job["status"]
        })

    except LookupError as exc:
        return jsonify({"error": str(exc)}), 404

    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 409

    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/sapiens/catalogo")
@require_admin
def api_sapiens_catalogo():
    """Catálogo lido do Senior pelo relay (produtos + derivações/cores). Referência
    para o gestor puxar as cores para o de-para sem digitar à mão."""
    cat = sapiens.carregar_catalogo_senior()
    produtos = [{"codpro": p.get("codpro") or p.get("codigo") or "",
                 "despro": p.get("despro") or p.get("nome") or "",
                 "marca": p.get("marca", ""),
                 "n_deriv": len(p.get("derivacoes") or [])}
                for p in (cat.get("produtos") or [])]
    return jsonify({"atualizado_em": cat.get("atualizado_em", ""),
                    "total": len(produtos), "produtos": produtos})


@app.route("/api/sapiens/importar-derivacoes", methods=["POST"])
@require_admin
def api_sapiens_importar_derivacoes():
    """Puxa as cores/derivações que o Senior tem para um código e grava no de-para
    do produto (nome do catálogo). Assim a Cor vira lista no carrinho, sem digitação."""
    d = request.get_json(force=True) or {}
    nome = (d.get("nome") or "").strip()
    codpro = (d.get("codpro") or "").strip()
    if not nome or not codpro:
        return jsonify({"error": "Informe o produto e o código."}), 400
    derivs = sapiens.derivacoes_do_catalogo(codpro)
    if not derivs:
        return jsonify({"error": "Esse código ainda não tem cores no catálogo do Senior. "
                        "Rode a sincronização do catálogo (relay) e tente de novo."}), 404
    m = sapiens.set_produto(nome, codigo=codpro, derivacoes=derivs)
    return jsonify({"ok": True, "n": len(derivs), "derivacoes": derivs, "map": m})


@app.route("/api/cores")
def api_cores():
    """Cores/derivações disponíveis por produto, para o dropdown de Cor no carrinho.
    Público (só nomes das cores, sem códigos) — o carrinho é usado sem login também."""
    resp = jsonify({"cores": sapiens.cores_por_produto()})
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp

def _adicionar_autoria_observacao(pedidos):
    """Acrescenta a identidade do usuário logado à observação Senior.

    Não altera a observação original do orçamento.
    A informação fica congelada no payload de cada envio.
    """
    vendedor = current_vendedor()

    if vendedor:
        usuario = str(vendedor.get("usuario") or "").strip()
        codigo = str(vendedor.get("cod_sapiens") or "").strip()
    else:
        usuario = "admin"
        codigo = ""

    autoria = f"Pedido gerado por: {usuario}"

    if codigo:
        autoria += f", código Sapiens: {codigo}"

    for pedido in pedidos:
        observacao_original = str(pedido.get("obs") or "").strip()

        if observacao_original:
            pedido["obs"] = f"{observacao_original} | {autoria}"
        else:
            pedido["obs"] = autoria


@app.route("/api/orcamento/<int:numero>/pedido-preview")
def api_pedido_preview(numero):
    """Monta o pedido da versão atual para revisão; não coloca nada na fila."""
    obj = load_orcamento(numero)
    if not obj:
        return jsonify({"error": "Orçamento não encontrado."}), 404
    if not _can_access(obj):
        return jsonify({"error": "Sem permissão."}), 403

    dono = find_vendedor(obj.get("vendedor", ""))
    pedidos, problemas = sapiens.montar_pedidos(
        obj, _versao_atual(obj), vendedor_cod=(dono or {}).get("cod_sapiens", ""))
    
    _adicionar_autoria_observacao(pedidos)

    versao_atual = obj.get("versao_atual", len(obj.get("versoes", [])))
    envios = _pf_envios(obj.get("pedido_fabrica"))
    ultimo = envios[-1] if envios else None
    versoes_enviadas = {
        envio.get("versao") for envio in envios if envio.get("versao") is not None
    }
    
    transacoes_disponiveis = sapiens.carregar_transacoes()
    codigos_validos = {
        t["codigo"] for t in transacoes_disponiveis
    }

    recentes, fixas = _transacoes_historicas(
        obj.get("pedido_fabrica")
    )

    for p in pedidos:
        marca = _marca_id(p.get("marca", ""))

        sugerida = (
            fixas.get(marca)
            or recentes.get(marca)
            or p["tnspro"]
        )

        p["tnspro"] = (
            sugerida
            if sugerida in codigos_validos or marca in fixas
            else sapiens.TRANSACAO_PADRAO
        )
    
    empresas_mantidas = _empresas_mantidas_no_senior(
        obj.get("pedido_fabrica"),
        pedidos,
        numero,
    )

    return jsonify({
        "pedidos": pedidos,
        "problemas": problemas,
        # Para a UI, ja_enviado continua significando o envio mais recente.
        "ja_enviado": ultimo,
        "versao_atual": versao_atual,
        "ultima_versao_enviada": (ultimo or {}).get("versao"),
        "pode_enviar": versao_atual not in versoes_enviadas,
        "n_envios": len(envios),
        "empresas_mantidas": empresas_mantidas,
        "transacoes_disponiveis": transacoes_disponiveis,
        "transacoes_fixas": fixas,
    })


@app.route("/api/orcamento/<int:numero>/enviar-fabrica", methods=["POST"])
def api_enviar_fabrica(numero):
    """Cria um NOVO envio imutável da versão atual para o relay.

    Regras:
    - a mesma versão nunca gera dois jobs;
    - cada versão nova gera novos job_ids;
    - jobs antigos ainda 'pendente' são marcados como 'substituido';
    - jobs já recebidos/processados permanecem no histórico.
    """
    with _orc_lock:
        obj = load_orcamento(numero)
        if not obj:
            return jsonify({"error": "Orçamento não encontrado."}), 404
        if not _can_access(obj):
            return jsonify({"error": "Sem permissão."}), 403

        versao_atual = obj.get("versao_atual", len(obj.get("versoes", [])))
        pf = _pf_para_historico(obj.get("pedido_fabrica"))
        envios = pf["envios"]

        if any(envio.get("versao") == versao_atual for envio in envios):
            return jsonify({
                "error": f"A versão v{versao_atual} já foi enviada à fábrica.",
                "versao": versao_atual,
            }), 409

        dono = find_vendedor(obj.get("vendedor", ""))
        pedidos, problemas = sapiens.montar_pedidos(
            obj, _versao_atual(obj), vendedor_cod=(dono or {}).get("cod_sapiens", ""))
        
        _adicionar_autoria_observacao(pedidos)

        if (problemas.get("sem_codigo") or problemas.get("sem_derivacao")
                or problemas.get("documento") or problemas.get("cliente_senior")):
            return jsonify({
                "error": "Pendências impedem o envio à fábrica.",
                "problemas": problemas,
            }), 400
        if not pedidos:
            return jsonify({"error": "Nenhum item para enviar."}), 400
        
        transacoes_solicitadas = (
            (request.get_json(silent=True) or {}).get("transacoes")
        )

        if not isinstance(transacoes_solicitadas, dict):
            return jsonify({
                "error": "Selecione a transação de cada empresa."
            }), 400

        codigos_validos = {
            t["codigo"] for t in sapiens.carregar_transacoes()
        }

        _, fixas = _transacoes_historicas(pf)

        for p in pedidos:
            marca = _marca_id(p.get("marca", ""))
            codigo = str(
                transacoes_solicitadas.get(marca) or ""
            ).strip()

            if marca in fixas and codigo != fixas[marca]:
                return jsonify({
                    "error": (
                        f"Transação de {marca} travada em {fixas[marca]} "
                        "por existir pedido Senior ou envio em processamento."
                    )
                }), 409

            if codigo not in codigos_validos and not (
                marca in fixas and codigo == fixas[marca]
            ):
                return jsonify({
                    "error": f"Transação {codigo!r} inválida para {marca}."
                }), 400

            p["tnspro"] = codigo

        # Uma empresa ausente da versão atual NÃO significa
        # que seu pedido anterior deva ser cancelado.
        # Apenas registramos que ele permanece sem alteração.
        empresas_mantidas = _empresas_mantidas_no_senior(
            pf, pedidos, numero
        )

        vendedor = current_vendedor()
        agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Se uma versão anterior ainda nem foi recebida pelo relay, ela é obsoleta.
        # Não faz sentido o Senior processá-la antes da versão mais nova.
        for envio_antigo in envios:
            for sub in envio_antigo.get("pedidos") or []:
                if sub.get("status") == "pendente":
                    sub["status"] = "substituido"
                    sub["substituido_em"] = agora
                    sub["substituido_por_versao"] = versao_atual

        envio_id = f"ORC-{numero}-V{versao_atual}"
        subpedidos = []

        for p in pedidos:
            marca_id = _marca_id(p.get("marca", ""))
            external_order_id = f"ORC-{numero}-{marca_id}"
            job_id = f"{envio_id}-{marca_id}"

            previous_senior_order_number = _latest_senior_number_for_external_id(
                pf, external_order_id
            )

            subpedidos.append({
                "job_id": job_id,
                "external_order_id": external_order_id,
                "marca": p["marca"],
                "empresa": p["codemp"],
                "payload": p,
                "status": "pendente",
                "recebido_em": None,
                "processado_em": None,
                "numero_pedido": None,
                "senior_order_number_anterior": previous_senior_order_number,
                "erro": None,
            })

        novo_envio = {
            "envio_id": envio_id,
            "solicitado_em": agora,
            "por": (vendedor["nome"] if vendedor else "admin"),
            "versao": versao_atual,
            "pedidos": subpedidos,
            "empresas_mantidas": empresas_mantidas,
        }

        envios.append(novo_envio)
        obj["pedido_fabrica"] = pf
        obj["atualizado_em"] = agora
        save_orcamento(obj)

    return jsonify({
        "ok": True,
        "status": "pendente",
        "versao": versao_atual,
        "envio_id": envio_id,
        "n_pedidos": len(subpedidos),
        "mensagem": f"Versão v{versao_atual} colocada na fila da fábrica.",
        "jobs": [{
            "job_id": s["job_id"],
            "external_order_id": s["external_order_id"],
            "marca": s["marca"],
        } for s in subpedidos],
        "pedidos": pedidos,
    })


@app.route("/api/orcamento/<int:numero>/fabrica-job/<job_id>/retry", methods=["POST"])
def api_fabrica_job_retry(numero, job_id):
    """Recoloca SOMENTE um subpedido falho na fila, sem criar nova versão.

    O payload/job_id permanecem idênticos: isto é um retry do mesmo envio, útil
    após corrigir cadastro/master data no Senior. Uma versão posterior da mesma
    empresa bloqueia o retry para evitar aplicar um snapshot antigo.
    """
    with _orc_lock:
        obj = load_orcamento(numero)
        if not obj or not obj.get("pedido_fabrica"):
            return jsonify({"error": "Orçamento/pedido não encontrado."}), 404
        if not _can_access(obj):
            return jsonify({"error": "Sem permissão."}), 403

        pf = obj["pedido_fabrica"]
        envio, alvo, _ = _find_relay_job(pf, job_id=job_id)
        if alvo is None:
            return jsonify({"error": "Job da fábrica não encontrado."}), 404

        status = alvo.get("status")
        if status != "erro":
            return jsonify({
                "error": "Somente um job com falha pode ser tentado novamente. Um job marcado como não reenviar é terminal nesta versão.",
                "status": status,
            }), 409

        if alvo.get("numero_pedido"):
            return jsonify({
                "error": "Este job já possui pedido Senior confirmado e não pode ser reenviado como retry.",
                "numero_pedido": alvo.get("numero_pedido"),
            }), 409

        if _has_newer_brand_submission(pf, envio, alvo):
            return jsonify({
                "error": "Existe uma versão mais nova desta empresa. Reabra o envio mais recente em vez de repetir uma versão antiga."
            }), 409

        agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        usuario = current_vendedor()
        por = usuario["nome"] if usuario else "admin"
        _registrar_retry_history(alvo, acao="retry", agora=agora, por=por)

        alvo["status"] = "pendente"
        alvo["erro"] = None
        alvo["recebido_em"] = None
        alvo["processado_em"] = None
        alvo["retry_autorizado"] = True
        alvo["retry_solicitado_em"] = agora
        alvo["retry_solicitado_por"] = por
        alvo["retry_count"] = int(alvo.get("retry_count") or 0) + 1

        obj["atualizado_em"] = agora
        save_orcamento(obj)

    return jsonify({
        "ok": True,
        "job_id": job_id,
        "status": "pendente",
        "mensagem": "Somente este pedido/empresa voltou para a fila do relay.",
    })


@app.route("/api/orcamento/<int:numero>/fabrica-job/<job_id>/nao-enviar", methods=["POST"])
def api_fabrica_job_nao_enviar(numero, job_id):
    """Encerra uma falha desta versão sem reenviá-la ao Senior.

    Isto NÃO cancela um pedido Senior existente; apenas resolve um subpedido que
    falhou e que o usuário decidiu não reenviar nesta versão.
    """
    with _orc_lock:
        obj = load_orcamento(numero)
        if not obj or not obj.get("pedido_fabrica"):
            return jsonify({"error": "Orçamento/pedido não encontrado."}), 404
        if not _can_access(obj):
            return jsonify({"error": "Sem permissão."}), 403

        pf = obj["pedido_fabrica"]
        envio, alvo, _ = _find_relay_job(pf, job_id=job_id)
        if alvo is None:
            return jsonify({"error": "Job da fábrica não encontrado."}), 404

        status = alvo.get("status")
        if status != "erro":
            return jsonify({
                "error": "Somente um job com falha pode ser marcado como não reenviar.",
                "status": status,
            }), 409

        if alvo.get("numero_pedido"):
            return jsonify({
                "error": "Este job já possui pedido Senior. Use um fluxo de cancelamento do ERP; não é seguro apenas ignorá-lo.",
                "numero_pedido": alvo.get("numero_pedido"),
            }), 409

        if _has_newer_brand_submission(pf, envio, alvo):
            return jsonify({
                "error": "Existe uma versão mais nova desta empresa; resolva o envio mais recente."
            }), 409

        agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        usuario = current_vendedor()
        por = usuario["nome"] if usuario else "admin"
        _registrar_retry_history(alvo, acao="nao_enviar", agora=agora, por=por)

        alvo["status"] = "nao_enviar"
        alvo["nao_enviar_em"] = agora
        alvo["nao_enviar_por"] = por
        alvo["retry_autorizado"] = False
        obj["atualizado_em"] = agora
        save_orcamento(obj)

    return jsonify({
        "ok": True,
        "job_id": job_id,
        "status": "nao_enviar",
        "pedido_status_atual": _pf_status(pf)["status"],
        "mensagem": "A falha foi encerrada sem reenviar esta empresa nesta versão.",
    })


# ------------------------- integração com o RELAY (agente on-premise) -------------------------
def _relay_ok():
    """Autentica o relay pelo token (header X-Relay-Token ou ?token=)."""
    tok = request.headers.get("X-Relay-Token") or request.args.get("token", "")
    valid = load_auth().get("relay_token", "")
    return bool(valid) and tok == valid


@app.route("/api/relay/pedidos-pendentes")
def api_relay_pendentes():
    """Retorna todos os jobs ainda pendentes em todo o histórico de envios."""
    if not _relay_ok():
        return jsonify({"error": "Token do relay inválido."}), 401

    pend = []
    for fn in os.listdir(ORC_DIR):
        if not fn.endswith(".json"):
            continue

        obj = load_orcamento(fn[:-5])
        if not obj:
            continue

        numero = obj["numero"]
        pf = obj.get("pedido_fabrica")

        for envio in _pf_envios(pf):
            subs = envio.get("pedidos") or []

            if subs:
                for i, sub in enumerate(subs):
                    if sub.get("status") != "pendente":
                        continue

                    marca = sub.get("marca", "")
                    pend.append({
                        "job_id": sub.get("job_id"),
                        "external_order_id": sub.get("external_order_id") or _external_order_id(numero, marca),
                        "envio_id": envio.get("envio_id"),
                        "numero": numero,
                        "versao": envio.get("versao"),
                        "indice": i,
                        "marca": marca,
                        "previous_senior_order_number": sub.get("senior_order_number_anterior"),
                        "payload": sub.get("payload"),
                    })

            elif envio.get("status") == "pendente":
                # Compatibilidade com um formato legado de payload único.
                pend.append({
                    "job_id": envio.get("job_id"),
                    "external_order_id": envio.get("external_order_id"),
                    "envio_id": envio.get("envio_id"),
                    "numero": numero,
                    "versao": envio.get("versao"),
                    "indice": None,
                    "marca": envio.get("marca", ""),
                    "payload": envio.get("payload"),
                })

    pend.sort(key=lambda x: (
        x.get("numero") or 0,
        x.get("versao") or 0,
        x.get("indice") if x.get("indice") is not None else -1,
    ))
    return jsonify({"pendentes": pend})


@app.route("/api/relay/pedido-recebido", methods=["POST"])
def api_relay_pedido_recebido():
    """Confirma que o relay persistiu o job localmente.

    Não significa que o Senior já recebeu o pedido.
    A chamada é idempotente: repetir o mesmo ACK não duplica nada.
    """
    if not _relay_ok():
        return jsonify({"error": "Token do relay inválido."}), 401

    d = request.get_json(force=True) or {}
    numero = d.get("numero")
    job_id = d.get("job_id")
    indice = d.get("indice")

    if numero is None:
        return jsonify({"error": "Número do orçamento não informado."}), 400

    with _orc_lock:
        obj = load_orcamento(numero)
        if not obj or not obj.get("pedido_fabrica"):
            return jsonify({"error": "Orçamento/pedido não encontrado."}), 404

        pf = obj["pedido_fabrica"]
        envio, alvo, indice_encontrado = _find_relay_job(pf, job_id=job_id, indice=indice)

        if alvo is None:
            if not job_id and len(_pf_envios(pf)) > 1:
                return jsonify({"error": "job_id é obrigatório para pedidos com histórico de envios."}), 400
            return jsonify({"error": "Job do relay não encontrado."}), 404

        status_atual = alvo.get("status", "pendente")

        if status_atual == "pendente":
            agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            alvo["status"] = "recebido_agente"
            alvo["recebido_em"] = agora
            save_orcamento(obj)
            status_atual = "recebido_agente"

        # Se o job foi substituído antes do ACK, o relay deve descartá-lo.
        processar = status_atual in ("recebido_agente", "processando_senior")

        return jsonify({
            "ok": True,
            "job_id": alvo.get("job_id"),
            "external_order_id": alvo.get("external_order_id") or _external_order_id(numero, alvo.get("marca", "")),
            "numero": numero,
            "versao": (envio or {}).get("versao"),
            "indice": indice_encontrado,
            "status": status_atual,
            "processar": processar,
            # O relay usa isto para permitir, de forma explícita, FAILED ->
            # ACKNOWLEDGED apenas quando o usuário pediu retry na web.
            "retry_autorizado": bool(alvo.get("retry_autorizado")),
        })


@app.route("/api/relay/pedido-resultado", methods=["POST"])
def api_relay_resultado():
    """Registra o resultado final de um job enviado ao Senior.

    job_id identifica exatamente uma versão/sub-pedido do histórico.
    Repetir o MESMO sucesso é idempotente; um número Senior diferente gera conflito.
    """
    if not _relay_ok():
        return jsonify({"error": "Token do relay inválido."}), 401

    d = request.get_json(force=True) or {}
    numero = d.get("numero")
    job_id = d.get("job_id")
    indice = d.get("indice")

    if numero is None:
        return jsonify({"error": "Número do orçamento não informado."}), 400

    with _orc_lock:
        obj = load_orcamento(numero)
        if not obj or not obj.get("pedido_fabrica"):
            return jsonify({"error": "Orçamento/pedido não encontrado."}), 404

        pf = obj["pedido_fabrica"]
        envio, alvo, indice_encontrado = _find_relay_job(pf, job_id=job_id, indice=indice)

        if alvo is None:
            if not job_id and len(_pf_envios(pf)) > 1:
                return jsonify({"error": "job_id é obrigatório para pedidos com histórico de envios."}), 400
            return jsonify({"error": "Job do relay não encontrado."}), 404

        status_atual = alvo.get("status", "pendente")
        if status_atual == "substituido":
            return jsonify({
                "error": "Este job foi substituído por uma versão mais nova e não deve ser processado.",
                "job_id": alvo.get("job_id"),
                "status": "substituido",
            }), 409

        agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if d.get("ok"):
            numero_pedido = d.get("numero_pedido")
            numero_anterior = alvo.get("senior_order_number_anterior")

            if numero_anterior and numero_pedido and str(numero_anterior) != str(numero_pedido):
                return jsonify({
                    "error": "Uma revisão deve manter o mesmo número de pedido Senior.",
                    "job_id": alvo.get("job_id"),
                    "numero_pedido_esperado": numero_anterior,
                    "numero_pedido_recebido": numero_pedido,
                }), 409

            if status_atual == "enviado":
                numero_existente = alvo.get("numero_pedido")
                if numero_existente and numero_pedido and str(numero_existente) != str(numero_pedido):
                    return jsonify({
                        "error": "O job já foi confirmado com outro número de pedido Senior.",
                        "job_id": alvo.get("job_id"),
                        "numero_pedido": numero_existente,
                    }), 409
            else:
                alvo["status"] = "enviado"
                alvo["numero_pedido"] = numero_pedido
                alvo["erro"] = None
                alvo["processado_em"] = agora
                alvo["retry_autorizado"] = False
                save_orcamento(obj)

        else:
            # Um erro posterior nunca pode apagar um sucesso já confirmado.
            if status_atual == "enviado":
                return jsonify({
                    "error": "O job já está confirmado como enviado e não pode ser rebaixado para erro.",
                    "job_id": alvo.get("job_id"),
                    "numero_pedido": alvo.get("numero_pedido"),
                }), 409

            alvo["status"] = "erro"
            alvo["erro"] = d.get("erro", "Falha no envio ao Sapiens.")
            alvo["processado_em"] = agora
            alvo["retry_autorizado"] = False
            save_orcamento(obj)

        return jsonify({
            "ok": True,
            "job_id": alvo.get("job_id"),
            "numero": numero,
            "versao": (envio or {}).get("versao"),
            "indice": indice_encontrado,
            "status": alvo.get("status"),
            "pedido_status_atual": _pf_status(pf)["status"],
            "numero_pedido": alvo.get("numero_pedido"),
        })
        

@app.route("/api/relay/produtos-index", methods=["POST"])
def api_relay_produtos_index():
    """Receive a complete, verified Homolog product index snapshot.

    Replaces only the requested company/environment product list.
    Does not alter derivation jobs or approved web-product mappings.
    """
    if not _relay_ok():
        return jsonify({"error": "Token do relay inválido."}), 401

    try:
        brand = senior_index.canonical_brand(
            request.args.get("marca", "")
        )
        environment = senior_index.validate_environment(
            request.args.get("ambiente", "producao")
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


    if request.headers.get("X-Sync-Complete") != "yes":
        return jsonify({
            "error": "Snapshot não identificado como completo."
        }), 400

    max_bytes = 8_000_000
    size = request.content_length

    if size is None or size < 1 or size > max_bytes:
        return jsonify({
            "error": "Tamanho do CSV ausente ou acima de 8 MB."
        }), 413

    raw = request.get_data(cache=False)

    if len(raw) != size:
        return jsonify({
            "error": "Upload incompleto."
        }), 400

    expected_hash = (
        request.headers.get("X-Sync-SHA256", "")
        .strip()
        .lower()
    )

    actual_hash = hashlib.sha256(raw).hexdigest()

    if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        return jsonify({
            "error": "SHA256 do snapshot ausente ou inválido."
        }), 400

    if actual_hash != expected_hash:
        return jsonify({
            "error": "SHA256 não corresponde ao CSV recebido."
        }), 400

    try:
        expected_count = int(
            request.headers.get("X-Sync-Total", "")
        )

        if not 1 <= expected_count <= 100000:
            raise ValueError("Quantidade fora dos limites.")

        text = raw.decode("utf-8-sig")

        reader = csv.DictReader(
            io.StringIO(text, newline="")
        )

        if reader.fieldnames != ["Produto", "Descrição"]:
            raise ValueError(
                "CSV deve conter exatamente Produto,Descrição."
            )

        seen = set()

        for row in reader:
            if None in row:
                raise ValueError(
                    "Linha CSV contém colunas inesperadas."
                )

            code = (row.get("Produto") or "").strip()
            description = (row.get("Descrição") or "").strip()

            if (
                not code or not description
                or len(code) > 80
                or len(description) > 1000
            ):
                raise ValueError(
                    "Produto ou descrição inválida no CSV."
                )

            if code in seen:
                raise ValueError(
                    "Código duplicado: " + code
                )

            seen.add(code)

        if len(seen) != expected_count:
            raise ValueError(
                f"Quantidade divergente: arquivo={len(seen)}, "
                f"declarado={expected_count}."
            )

        previous = next(
            (
                item["total"]
                for item in senior_index.metadata()
                if item["brand"] == brand
                and item["environment"] == environment
            ),
            0
        )

        fd, temporary = tempfile.mkstemp(
            prefix="senior_index_",
            suffix=".csv",
            dir=DATA_DIR
        )

        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)

            imported = senior_index.import_csv(
                temporary,
                brand,
                environment,
                source="Senior SOAP Produção"
            )

        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

        return jsonify({
            "ok": True,
            "ambiente": environment,
            "marca": brand,
            "anteriores": previous,
            "importados": imported,
            "sha256": actual_hash
        })

    except (ValueError, UnicodeError, csv.Error) as exc:
        return jsonify({
            "error": str(exc)
        }), 400



@app.route("/api/relay/catalogo", methods=["POST"])
def api_relay_catalogo():
    """O relay envia aqui o catálogo lido do Sapiens (produtos + derivações + preços)
    para alimentarmos o de-para. Guarda como referência (não sobrescreve o de-para manual)."""
    if not _relay_ok():
        return jsonify({"error": "Token do relay inválido."}), 401
    d = request.get_json(force=True) or {}
    n = sapiens.salvar_catalogo_senior(d.get("produtos") or [])
    return jsonify({"ok": True, "recebidos": n})


@app.route("/api/relay/ping")
def api_relay_ping():
    """Health check do relay."""
    if not _relay_ok():
        return jsonify({"error": "Token do relay inválido."}), 401
    return jsonify({"ok": True, "hora": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})


@app.route("/api/relay-token")
@require_admin
def api_relay_token():
    return jsonify({"token": load_auth().get("relay_token", "")})


@app.route("/api/relatorio-vendas")
@require_admin
def api_relatorio_vendas():
    """Vendas (orçamentos com status 'ganho') por MÊS, separadas por marca WJ e Bella Itália.
    O mês é o de quando virou 'ganho' (ganho_em); cai para criado_em em orçamentos antigos."""
    meses = {}
    for fn in os.listdir(ORC_DIR):
        if not fn.endswith(".json"):
            continue
        obj = load_orcamento(fn[:-5])
        if not obj or obj.get("status") != "ganho":
            continue
        quando = obj.get("ganho_em") or obj.get("atualizado_em") or obj.get("criado_em") or ""
        mes = quando[:7]  # YYYY-MM
        wj, bella = _split_marca(_versao_atual(obj))
        m = meses.setdefault(mes, {"mes": mes, "wj": 0.0, "bella": 0.0, "total": 0.0, "n": 0})
        m["wj"] += wj
        m["bella"] += bella
        m["total"] += wj + bella
        m["n"] += 1
    out = sorted(meses.values(), key=lambda x: x["mes"], reverse=True)
    for m in out:
        m["wj"] = round(m["wj"], 2)
        m["bella"] = round(m["bella"], 2)
        m["total"] = round(m["total"], 2)
    return jsonify({"vendas": out})


@app.route("/api/leads")
@require_admin
def api_leads():
    """Lista completa dos orçamentos registrados (mais recentes primeiro)."""
    rows = read_leads()
    rows.reverse()
    return jsonify({"leads": rows})


def _serve_csv(path, columns, filename):
    """Serve um CSV (leads ou itens) autenticado por token na URL ou admin logado."""
    token = request.args.get("token", "")
    valid = load_auth().get("powerbi_token", "")
    if not (admin_logged_in() or (valid and token == valid)):
        return jsonify({"error": "Token inválido ou ausente."}), 401
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            content = f.read()
    else:
        content = ";".join(columns) + "\n"   # cabeçalho vazio se ainda não há dados
    data = "﻿" + content   # BOM: acentos corretos no Power BI/Excel
    return Response(data, mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"',
                             "Cache-Control": "no-cache, no-store, must-revalidate"})


@app.route("/api/leads.csv")
def api_leads_csv():
    """Planilha de orçamentos (1 linha por orçamento) para o Power BI."""
    return _serve_csv(LEADS_FILE, LEAD_COLUMNS, "leads.csv")


@app.route("/api/itens.csv")
def api_itens_csv():
    """Planilha de itens (1 linha por produto por orçamento) para o Power BI."""
    return _serve_csv(ITENS_FILE, ITENS_COLUMNS, "itens.csv")


@app.route("/api/orcamentos.csv")
def api_orcamentos_csv():
    """Planilha de orçamentos salvos com STATUS atual (aberto/ganho/perdido) — Power BI."""
    return _serve_csv(ORC_CSV_FILE, ORC_COLUMNS, "orcamentos.csv")


@app.route("/api/powerbi-token")
@require_admin
def api_powerbi_token():
    return jsonify({"token": load_auth().get("powerbi_token", "")})


@app.route("/api/powerbi-token", methods=["POST"])
@require_admin
def api_powerbi_regen():
    data = load_auth()
    data["powerbi_token"] = secrets.token_urlsafe(24)
    save_auth(data)
    return jsonify({"token": data["powerbi_token"]})


@app.route("/")
def index():
    return render_page("index.html")


@app.route("/admin")
def admin():
    if not password_set():
        return Response(
            "<h2 style='font-family:sans-serif'>Senha do admin não configurada.</h2>"
            "<p style='font-family:sans-serif'>No servidor, rode uma vez: "
            "<code>python app/set_password.py</code></p>",
            mimetype="text/html",
        )
    if not admin_logged_in():
        return render_page("login.html")
    return render_page("admin.html")


@app.route("/api/login", methods=["POST"])
def api_login():
    pw = (request.get_json(force=True) or {}).get("password", "")
    data = load_auth()
    if "hash" in data and check_password_hash(data["hash"], pw):
        session["admin"] = True
        return jsonify({"ok": True})
    return jsonify({"error": "Senha incorreta."}), 401


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.pop("admin", None)
    return jsonify({"ok": True})


# estado da reindexação (roda em segundo plano para não estourar o tempo limite)
_reindex = {"running": False, "done": False, "error": None, "summary": None, "msg": ""}


def _do_reindex(tmp, old_names):
    global _reader, _closing
    try:
        # libera o PDF atual da memória antes de reindexar
        if _reader is not None:
            try:
                _reader.stream.close()
            except Exception:
                pass
            _reader = None
        _closing = None

        _reindex["msg"] = "Fazendo backup do modelo atual..."
        os.makedirs(BACKUPS, exist_ok=True)
        if os.path.exists(SOURCE_PDF):
            stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
            shutil.move(SOURCE_PDF, os.path.join(BACKUPS, f"MODELO_{stamp}.pdf"))
            for b in sorted(os.listdir(BACKUPS))[:-3]:
                os.remove(os.path.join(BACKUPS, b))

        shutil.move(tmp, SOURCE_PDF)
        summary = catalog_builder.build(SOURCE_PDF, BASE,
                                        progress=lambda m: _reindex.update(msg=m))
        load_catalog()
        new_names = set(summary.pop("names"))
        summary["added"] = sorted(new_names - old_names)
        summary["removed"] = sorted(old_names - new_names)
        _reindex.update(running=False, done=True, summary=summary, msg="Concluído.")
    except Exception as e:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass
        _reindex.update(running=False, done=True, error=str(e), msg="Erro.")


@app.route("/api/update", methods=["POST"])
@require_admin
def update_catalog():
    """Recebe o novo PDF e dispara a reindexação em segundo plano."""
    if _reindex["running"]:
        return jsonify({"error": "Já existe uma atualização em andamento."}), 409
    f = request.files.get("pdf")
    if not f or not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Envie um arquivo .pdf."}), 400

    tmp = os.path.join(ROOT, "_upload_tmp.pdf")
    f.save(tmp)
    # valida pelo cabeçalho (%PDF) sem carregar o arquivo inteiro na memória
    with open(tmp, "rb") as fh:
        if fh.read(5) != b"%PDF-":
            os.remove(tmp)
            return jsonify({"error": "O arquivo enviado não é um PDF válido."}), 400

    old_names = {p["name"] for p in CATALOG["products"]}
    _reindex.update(running=True, done=False, error=None, summary=None, msg="Iniciando...")
    threading.Thread(target=_do_reindex, args=(tmp, old_names), daemon=True).start()
    return jsonify({"started": True})


@app.route("/api/update/status")
@require_admin
def update_status():
    return jsonify(_reindex)


# Reinício do serviço pelo /admin. Só fica ativo se WJ_SERVICE_NAME estiver definido
# (ex.: "orcamento-v3") E houver uma regra sudo permitindo www-data reiniciar esse
# serviço específico. O nome do serviço vem do ambiente (nunca do cliente).
SYSTEMCTL = os.environ.get("WJ_SYSTEMCTL", "/usr/bin/systemctl")
SERVICE_NAME = os.environ.get("WJ_SERVICE_NAME", "").strip()


@app.route("/api/restart", methods=["GET", "POST"])
@require_admin
def api_restart():
    if request.method == "GET":
        return jsonify({"enabled": bool(SERVICE_NAME)})
    if not SERVICE_NAME:
        return jsonify({"error": "Reinício não configurado neste ambiente."}), 400
    # confirma a permissão de sudo antes de prometer o reinício
    try:
        chk = subprocess.run(["sudo", "-n", SYSTEMCTL, "is-active", SERVICE_NAME],
                             capture_output=True, text=True, timeout=8)
    except Exception as e:
        return jsonify({"error": f"Falha ao chamar o systemctl: {e}"}), 500
    if "password is required" in (chk.stderr or "").lower():
        return jsonify({"error": "Sem permissão de sudo para reiniciar "
                        "(falta a regra em /etc/sudoers.d/)."}), 500
    # dispara o restart com 1s de atraso: o navegador recebe a resposta antes do processo cair
    subprocess.Popen(["/bin/sh", "-c", f"sleep 1; sudo -n {SYSTEMCTL} restart {SERVICE_NAME}"])
    return jsonify({"ok": True})


@app.route("/api/catalog")
def api_catalog():
    resp = jsonify(CATALOG)
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


@app.route("/api/generate", methods=["POST"])
def generate():
    v = current_vendedor()   # login é OPCIONAL: sem login gera o PDF mas não salva na conta
    data = request.get_json(force=True)
    client = (data.get("client") or "").strip()
    architect = (data.get("architect") or "").strip()
    whatsapp = (data.get("whatsapp") or "").strip()
    cidade = (data.get("city") or "").strip()
    observacao = (data.get("observacao") or "").strip()
    documento = (data.get("documento") or "").strip()
    mode = data.get("mode") or "full"  # "full" = apresentação completa | "budget" = só orçamento
    # o vendedor vem sempre da sessão (não do cliente); admin sem login de vendedor cai como avulso
    if v:
        seller, owner = v["nome"], v["usuario"]
    else:
        seller, owner = (data.get("seller") or "").strip(), ""
    raw_items = data.get("items") or []
    if not raw_items:
        return jsonify({"error": "Nenhum produto selecionado."}), 400

    items = _build_items(raw_items)
    if not items:
        return jsonify({"error": "Produtos inválidos."}), 400

    total = sum(i["price"] * i["qty"] for i in items)
    out = build_pdf(client, items, total, mode, observacao)

    # registra o orçamento (resumo + itens) — não interrompe o download se falhar.
    # o store editável (data/orcamentos/) só é criado quando há vendedor logado.
    try:
        numero = append_lead(seller, client, architect, whatsapp, cidade, mode, items, total,
                             observacao, documento)
        if owner:
            create_orcamento(numero, owner, client, architect, whatsapp, cidade, mode, items,
                             observacao, documento, data.get("cod_cli_senior", ""))
    except Exception:
        pass

    safe_client = re.sub(r"[^\w \-]", "", client)[:60].strip()
    fname = "Orçamento WJ" if mode == "budget" else "Apresentação WJ"
    if safe_client:
        fname += f" - {safe_client}"
    fname += f" - {date.today().strftime('%d-%m-%Y')}.pdf"
    return send_file(out, mimetype="application/pdf", as_attachment=True, download_name=fname)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"Sistema de orçamentos: http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)

