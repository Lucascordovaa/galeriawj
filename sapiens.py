# -*- coding: utf-8 -*-
"""Integração com o ERP Sapiens (Senior) — envio de PEDIDO DE VENDA a partir do orçamento.

FASE 0 (atual): monta o pedido e opera em MODO SIMULAÇÃO — NÃO envia nada de verdade,
apenas devolve o payload que *seria* enviado, para o TI da fábrica validar contra o
web service com.senior.g5.co.mcm.ven.pedidos. O envio real (Fase 1) só liga quando
houver acesso de rede + credenciais + de-para de produtos preenchido.

Guarda tudo isolado em app/data/ (sapiens_config.json e sapiens_map.json), keyado pelo
NOME do produto (estável entre reindexações do catálogo, igual à migração do carrinho).
"""
import json
import os
import re

# "simulacao" (padrão) nunca toca a rede; "real" fica para a Fase 1.
MODO = os.environ.get("WJ_SAPIENS_MODO", "simulacao")

# Código de transação do Sapiens para os pedidos da Galeria WJ (fixo, existente no Senior).
TRANSACAO_PADRAO = "90119"


def _digitos(s):
    return re.sub(r"\D", "", s or "")


def _cpf_ok(c):
    if len(c) != 11 or c == c[0] * 11:
        return False
    for i in (9, 10):
        s = sum(int(c[j]) * ((i + 1) - j) for j in range(i))
        dv = (s * 10) % 11 % 10
        if dv != int(c[i]):
            return False
    return True


def _cnpj_ok(c):
    if len(c) != 14 or c == c[0] * 14:
        return False
    p1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    for pesos, i in ((p1, 12), ([6] + p1, 13)):
        s = sum(int(c[j]) * pesos[j] for j in range(i))
        dv = s % 11
        dv = 0 if dv < 2 else 11 - dv
        if dv != int(c[i]):
            return False
    return True


def documento_valido(doc):
    """Valida CPF (11) ou CNPJ (14) pelos dígitos verificadores."""
    d = _digitos(doc)
    return _cpf_ok(d) if len(d) == 11 else (_cnpj_ok(d) if len(d) == 14 else False)


def documento_fmt(doc):
    d = _digitos(doc)
    if len(d) == 11:
        return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}"
    if len(d) == 14:
        return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"
    return doc or ""

_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
CONFIG_FILE = os.path.join(_DATA, "sapiens_config.json")
MAP_FILE = os.path.join(_DATA, "sapiens_map.json")
CATALOGO_FILE = os.path.join(_DATA, "sapiens_catalogo.json")  # catálogo lido do Senior pelo relay

TRANSACOES_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "senior_transacoes.json"
)


def carregar_transacoes():
    """Lista de transações permitidas no envio à fábrica."""
    with open(TRANSACOES_FILE, encoding="utf-8") as arquivo:
        registros = json.load(arquivo)

    if not isinstance(registros, list):
        raise ValueError("senior_transacoes.json deve conter uma lista.")

    resultado = []
    vistos = set()

    for item in registros:
        codigo = str(item.get("codigo") or "").strip()
        descricao = str(item.get("descricao") or "").strip()

        if (
            not re.fullmatch(r"[0-9]{5}", codigo)
            or not descricao
            or codigo in vistos
        ):
            raise ValueError(
                f"Transação inválida ou repetida: {codigo!r}."
            )

        vistos.add(codigo)
        resultado.append({
            "codigo": codigo,
            "descricao": descricao
        })

    if not resultado:
        raise ValueError("Nenhuma transação cadastrada.")

    return resultado


def salvar_catalogo_senior(produtos):
    """Guarda o catálogo (produtos+derivações+preços) que o relay leu do Sapiens.
    É a referência para o gestor casar com o de-para (não sobrescreve o de-para manual)."""
    dados = {"atualizado_em": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             "produtos": produtos or []}
    with open(CATALOGO_FILE, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=1)
    return len(dados["produtos"])


def carregar_catalogo_senior():
    if os.path.exists(CATALOGO_FILE):
        try:
            with open(CATALOGO_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"atualizado_em": "", "produtos": []}
    return {"atualizado_em": "", "produtos": []}


def _norm_cod(c):
    """Normaliza um código para casar formatos (WS '001030140' vs ODBC '00.10.30.140')."""
    return re.sub(r"[^0-9A-Za-z]", "", str(c or "")).lstrip("0") or "0"


def catalogo_index():
    """{ código normalizado -> produto do catálogo Senior }, p/ achar por codpro."""
    idx = {}
    for p in (carregar_catalogo_senior().get("produtos") or []):
        cod = p.get("codpro") or p.get("codigo") or ""
        if cod:
            idx[_norm_cod(cod)] = p
    return idx


def derivacoes_do_catalogo(codpro):
    """Derivações (cores) que o Senior tem para um código — [{cod, nome}] normalizadas.
    Vazio se o código ainda não veio no catálogo sincronizado pelo relay."""
    p = catalogo_index().get(_norm_cod(codpro))
    return _norm_derivacoes((p or {}).get("derivacoes")) if p else []

# parâmetros fixos do pedido (o TI da fábrica define os valores corretos).
# Empresa/filial são POR MARCA — WJ e Bella Itália são empresas diferentes no Senior,
# e um orçamento com as duas marcas vira DOIS pedidos (um por empresa).
CONFIG_CAMPOS = ["empresaWJ", "filialWJ", "empresaBella", "filialBella",
                 "tipoOperacao", "transacao", "codred",
                 "condicaoPagamento", "tabelaPreco", "vendedor", "baseUrl", "usuario"]

# padrões confirmados: WJ = empresa 10, Bella Itália = empresa 1, filial 1 nas duas
CONFIG_PADRAO = {"empresaWJ": "10", "filialWJ": "1", "empresaBella": "1", "filialBella": "1"}


def load_config():
    cfg = {k: "" for k in CONFIG_CAMPOS}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                cfg.update({k: v for k, v in json.load(f).items() if k in CONFIG_CAMPOS})
        except Exception:
            pass
    if not cfg.get("transacao"):
        cfg["transacao"] = TRANSACAO_PADRAO   # mantido por compatibilidade; o payload usa 90119 fixo
    for k, v in CONFIG_PADRAO.items():
        if not cfg.get(k):
            cfg[k] = v
    return cfg


def save_config(d):
    cfg = load_config()
    for k in CONFIG_CAMPOS:
        if k in d:
            cfg[k] = str(d.get(k) or "").strip()
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)
    return cfg


def load_map():
    """De-para: { "<nome do produto>": {codigo, derivacao, unidade} }."""
    if os.path.exists(MAP_FILE):
        try:
            with open(MAP_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_map(m):
    with open(MAP_FILE, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=1)


def _norm_derivacoes(lista):
    """Normaliza a lista de derivações: [{cod, nome}] (nome = a cor/acabamento)."""
    out = []
    for d in (lista or []):
        cod = str(d.get("cod") or d.get("derivacao") or "").strip()
        nome = str(d.get("nome") or d.get("cor") or "").strip()
        if cod or nome:
            out.append({"cod": cod, "nome": nome})
    return out


def set_produto(nome, codigo=None, unidade=None, derivacoes=None):
    """Atualização PARCIAL do de-para de um produto. Passe só os campos que quer mudar.
    Um produto sem código é removido (não dá para lançar pedido sem código).
    derivacoes = lista [{cod, nome}] — cada derivação é uma cor/acabamento no Sapiens.
    (No futuro, essa lista será preenchida automaticamente puxando do Senior.)"""
    m = load_map()
    nome = (nome or "").strip()
    if not nome:
        return m
    cur = dict(m.get(nome) or {})
    if codigo is not None:
        cur["codigo"] = str(codigo).strip()
    if unidade is not None:
        cur["unidade"] = str(unidade).strip()
    if derivacoes is not None:
        cur["derivacoes"] = _norm_derivacoes(derivacoes)
    if not cur.get("codigo"):
        m.pop(nome, None)
    else:
        m[nome] = {"codigo": cur.get("codigo", ""), "unidade": cur.get("unidade", ""),
                   "derivacoes": cur.get("derivacoes", [])}
    _save_map(m)
    return m


def cores_por_produto(mapa=None):
    """{ '<nome do produto>': ['Bronze','Preto',...] } — só os nomes das derivações,
    para o carrinho montar o dropdown de Cor. Não expõe códigos."""
    mapa = mapa or load_map()
    out = {}
    for nome, info in mapa.items():
        nomes = [d.get("nome", "") for d in (info.get("derivacoes") or []) if d.get("nome")]
        if nomes:
            out[nome] = nomes
    return out


def _marca_key(marca):
    return "bella" if "bella" in (marca or "").lower() else "wj"


def montar_pedidos(orc, versao, config=None, mapa=None, vendedor_cod=None):
    """Monta os pedidos de venda da versão atual, separados por empresa/marca.

    O payload desta função é o contrato WEB -> RELAY. Ele contém somente os dados
    de negócio que o relay precisa para montar o SOAP real. Defaults comerciais do
    cliente (representante, condição/forma de pagamento, transportadora etc.) ficam
    sob responsabilidade do Senior e NÃO são enviados pela web.
    """
    config = config or load_config()
    mapa = mapa or load_map()

    doc = _digitos(orc.get("documento", ""))
    documento_problema = (
        "CPF/CNPJ do cliente inválido."
        if doc and not documento_valido(doc)
        else ""
    )

    cod_cli_senior = str(orc.get("cod_cli_senior") or "").strip()
    cliente_senior_problema = (
        "Informe o código do cliente no Senior antes de enviar à fábrica."
        if not cod_cli_senior
        else ""
    )

    grupos = {}  # 'wj' | 'bella' -> lista de itens montados
    sem_codigo, sem_derivacao = [], []

    for it in versao.get("itens", []):
        nome = it.get("produto", "")
        m = mapa.get(nome) or {}
        codigo = str(m.get("codigo", "") or "").strip()

        if not codigo:
            sem_codigo.append(nome)

        cor = (it.get("cor", "") or "").strip()
        derivs = m.get("derivacoes") or []
        derivacao = ""

        if derivs:
            match = next(
                (
                    d for d in derivs
                    if d.get("nome", "").strip().lower() == cor.lower()
                ),
                None,
            )
            if match:
                derivacao = str(match.get("cod", "") or "").strip()

        # Para o fluxo que validamos em Homolog, codDer faz parte do item.
        # Não deixamos o relay descobrir isso só depois do envio.
        if codigo and not derivacao:
            sem_derivacao.append(
                f"{nome}" + (f" / {cor}" if cor else " (sem cor/derivação)")
            )

        qtd = int(it.get("qtd", 0) or 0)
        preco = round(float(it.get("preco", 0) or 0), 2)

        grupos.setdefault(_marca_key(it.get("marca", "")), []).append({
            "produto": nome,
            "codpro": codigo,
            "codder": derivacao,
            "desder": cor,
            "qtdped": qtd,
            "preuni": preco,
            "total": round(preco * qtd, 2),
        })

    cabecalho_cliente = {
        "codcli": cod_cli_senior or None,
        "nome": orc.get("cliente", ""),
        "cgccpf": doc,
        "cgccpfFmt": documento_fmt(doc),
        "cidade": orc.get("cidade", ""),
    }

    marcas = {
        "wj": ("WJ", config.get("empresaWJ", "10"), config.get("filialWJ", "1")),
        "bella": ("Bella Itália", config.get("empresaBella", "1"), config.get("filialBella", "1")),
    }

    numero_orcamento = orc.get("numero")
    pedidos = []

    for key in ("wj", "bella"):
        itens = grupos.get(key)
        if not itens:
            continue

        marca_nome, codemp, codfil = marcas[key]

        pedidos.append({
            "marca": marca_nome,
            "servico": "com.senior.g5.co.mcm.ven.pedidos",
            "codemp": str(codemp or "").strip(),
            "codfil": str(codfil or "").strip(),
            "tnspro": str(
                config.get("transacao") or TRANSACAO_PADRAO
            ).strip(),
            "pedcli": f"PED {numero_orcamento}",
            "cliente": dict(cabecalho_cliente),
            "numeroOrcamento": numero_orcamento,
            "obs": (orc.get("observacao", "") or "").strip(),
            "itens": itens,
            "valorTotal": round(sum(i["total"] for i in itens), 2),
        })

    return pedidos, {
        "sem_codigo": sem_codigo,
        "sem_derivacao": sem_derivacao,
        "documento": documento_problema,
        "cliente_senior": cliente_senior_problema,
    }


def enviar_pedido(payload):
    """FASE 0: MODO SIMULAÇÃO — não envia; devolve o que seria enviado.
    FASE 1 (futuro): aqui entra a chamada real (JSON Facade Adapter ou SOAP)."""
    if MODO != "real":
        return {"ok": True, "modo": "simulacao", "numero_pedido": None, "payload": payload}
    return {"ok": False, "modo": "real",
            "erro": "Envio real ainda não configurado (Fase 1)."}
