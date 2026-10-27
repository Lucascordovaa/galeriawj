# -*- coding: utf-8 -*-
"""
Indexa o catálogo (PDF com 1 página por produto, cada uma com o preço impresso).
  - de cada página extrai: categoria + nome, descrição e preço (R$)
  - detecta automaticamente capa, divisor "Bella Itália" e página de encerramento
  - gera miniaturas para o app web
"""
import json
import os
import re
import shutil
import time
import unicodedata

from pypdf import PdfReader
import pypdfium2 as pdfium


def norm(s):
    s = unicodedata.normalize("NFD", s)
    return "".join(c for c in s if unicodedata.category(c) != "Mn").lower()


def is_spaced(line):
    """A linha está com letras espaçadas? Ex.: 'A R A N D E L A'."""
    toks = line.split(" ")
    ones = sum(1 for t in toks if len(t) == 1)
    return len(toks) >= 2 and ones >= len(toks) * 0.6


def despace(line):
    """Converte 'A R A N D E L A  Á P I C E' -> 'ARANDELA ÁPICE'."""
    return " ".join(w.replace(" ", "") for w in re.split(r"\s{2,}", line.strip()))


def parse_money(s):
    """'R$ 8.400,00' -> 8400.0"""
    s = s.replace("R$", "").replace(" ", "").strip().replace(".", "").replace(",", ".")
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def clean_line(line):
    """Limpa a linha conforme o próprio espaçamento: se estiver com letras
    espaçadas ('A L O  1 2 0'), des-espaça respeitando o espaço duplo entre
    palavras; se já for texto normal ('ALO 120CM'), mantém como está."""
    return despace(line) if is_spaced(line) else line.strip()


def parse_page(raw):
    """Retorna (nome_completo, descricao, preco) ou None se não for página de produto.

    Funciona nos dois formatos do PDF:
      - Original: nome com letras espaçadas, categoria em texto normal.
      - Comprimido: nome em texto normal, categoria com letras espaçadas.
    O formato é detectado pelas linhas de descrição (se estão ou não espaçadas).
    """
    if not raw or "R$" not in raw.replace(" ", ""):
        return None
    lines = [l for l in raw.splitlines() if l.strip()]
    if not lines:
        return None
    spaced = [is_spaced(l) for l in lines]
    cleaned = [clean_line(l) for l in lines]

    def is_up(c):
        return c == c.upper() and any(ch.isalpha() for ch in c)

    up = [is_up(c) for c in cleaned]

    price_idx = next((i for i, c in enumerate(cleaned) if c.replace(" ", "").startswith("R$")), None)
    if price_idx is None:
        return None
    price = parse_money(cleaned[price_idx])
    if price is None:
        return None

    # descrição = linhas em caixa mista (não caixa-alta), fora o preço
    desc_idx = [i for i in range(len(lines)) if i != price_idx and not up[i]]
    # detecta o formato: as linhas de descrição estão com letras espaçadas?
    desc_spaced = sum(spaced[i] for i in desc_idx) > len(desc_idx) / 2 if desc_idx else False

    # linhas em CAIXA ALTA (fora o preço) = nome + categoria
    up_idx = [i for i in range(len(lines)) if up[i] and i != price_idx]
    if desc_spaced:                 # ORIGINAL: nome espaçado, categoria normal
        name_parts = [cleaned[i] for i in up_idx if spaced[i]]
        cat_parts = [cleaned[i] for i in up_idx if not spaced[i]]
    else:                           # COMPRIMIDO: nome normal, categoria espaçada
        name_parts = [cleaned[i] for i in up_idx if not spaced[i]]
        cat_parts = [cleaned[i] for i in up_idx if spaced[i]]

    name = " ".join(name_parts).strip()
    if not name:
        return None
    category = re.sub(r"\s*\|\s*", " | ", cat_parts[0]) if cat_parts else ""
    desc = " ".join(cleaned[i] for i in desc_idx)

    full = (category + " " + name).strip()
    return full, desc.strip(), price


def is_white_page(doc, page_num):
    """Página praticamente branca (divisor de seção, ex.: 'BELLA ITALIA')."""
    img = doc[page_num - 1].render(scale=0.06).to_pil().convert("L")
    px = list(img.getdata())
    white = sum(1 for v in px if v > 240)
    return white / len(px) > 0.95


def build(pdf_path, app_dir, progress=lambda msg: None):
    """Reindexa o catálogo. Retorna resumo {products, wj, bella, divider...}."""
    static_dir = os.path.join(app_dir, "static")
    thumb_dir = os.path.join(static_dir, "thumbs")

    progress("Lendo texto do PDF...")
    reader = PdfReader(pdf_path)
    n = len(reader.pages)
    if n < 3:
        raise ValueError("PDF com menos de 3 páginas — não parece ser o catálogo.")

    page_text = {}
    for i in range(n):
        try:
            page_text[i + 1] = reader.pages[i].extract_text() or ""
        except Exception:
            page_text[i + 1] = ""

    # libera a memória do pypdf antes de renderizar as miniaturas (pico de RAM menor)
    try:
        reader.stream.close()
    except Exception:
        pass
    del reader

    # capa = primeira página; encerramento = última página com "OBRIGADO" (ou a última)
    cover = 1
    closing = n
    for pg in range(n, max(n - 5, 1), -1):
        if "obrigado" in norm(page_text.get(pg, "").replace(" ", "")):
            closing = pg
            break

    progress("Procurando a página SEPARADOR (WJ / Bella Itália)...")
    doc = pdfium.PdfDocument(pdf_path)
    divider = None
    for pg in range(2, closing):
        if "separador" in re.sub(r"\s", "", norm(page_text.get(pg, ""))):
            divider = pg
            break

    progress("Montando lista de produtos...")
    skip = {cover, closing}
    if divider:
        skip.add(divider)
    products = []
    for pg in range(2, closing):
        if pg in skip:
            continue
        parsed = parse_page(page_text.get(pg, ""))
        if not parsed:
            continue  # página inesperada — ignora com segurança
        name, desc, price = parsed
        products.append({
            "name": name,
            "description": desc,
            "price": price,
            "brand": "WJ" if (not divider or pg < divider) else "Bella Itália",
            "pages": [pg],
        })

    if not products:
        raise ValueError("Nenhum produto encontrado no PDF — estrutura inesperada.")

    # nomes duplicados: primeiro tenta distinguir pelo material descrito
    # (ex.: versão "couro ou camurça natural" vs versão em alumínio/pintura).
    groups = {}
    for p in products:
        groups.setdefault(norm(p["name"]), []).append(p)
    for group in groups.values():
        if len(group) > 1:
            for p in group:
                nm, ds = norm(p["name"]), norm(p["description"])
                if ("couro" in ds or "camurca" in ds) and "couro" not in nm and "camurca" not in nm:
                    p["name"] += " (COURO/CAMURÇA)"

    # o que ainda restou com nome idêntico -> sufixo romano
    seen = {}
    for p in products:
        seen.setdefault(norm(p["name"]), []).append(p)
    rom = ["I", "II", "III", "IV", "V", "VI", "VII", "VIII"]
    for group in seen.values():
        if len(group) > 1:
            for i, p in enumerate(group):
                p["name"] = f"{p['name']} ({rom[i] if i < len(rom) else i + 1})"

    # versão desta reindexação — entra no NOME do arquivo das miniaturas, para o
    # navegador nunca reutilizar imagem antiga (a URL muda a cada atualização)
    version = int(time.time())

    for i, p in enumerate(products):
        p["id"] = i + 1
        p["search"] = norm(p["name"] + " " + p["description"])
        p["thumb"] = f"thumbs/{version}-{p['id']}.jpg"

    progress(f"{len(products)} produtos. Gerando miniaturas...")
    if os.path.isdir(thumb_dir):
        shutil.rmtree(thumb_dir)
    os.makedirs(thumb_dir, exist_ok=True)
    for p in products:
        img = doc[p["pages"][0] - 1].render(scale=0.45).to_pil()
        img.save(os.path.join(thumb_dir, f"{version}-{p['id']}.jpg"), quality=78)
    doc.close()

    catalog = {
        "source": os.path.basename(pdf_path),
        "version": version,
        "coverPage": cover,
        "bellaDividerPage": divider,
        "closingPage": closing,
        "products": products,
    }
    with open(os.path.join(static_dir, "catalog.json"), "w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=1)

    wj = sum(1 for p in products if p["brand"] == "WJ")
    return {
        "pages": n,
        "products": len(products),
        "wj": wj,
        "bella": len(products) - wj,
        "divider": divider,
        "closing": closing,
        "names": [p["name"] for p in products],
    }
