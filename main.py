from typing import Optional, List, Any, Dict

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from urllib.parse import quote
from fastapi.middleware.cors import CORSMiddleware
import unicodedata



app = FastAPI(
    title="Backend Maria Dolores",
    description="Proxy para API VTEX da Maria Dolores com enriquecimento de dados",
    version="0.1.0",
)

# 🔓 CORS liberado para qualquer origem (inclui Base44, localhost, etc.)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],      # libera geral
    allow_credentials=False,  # importante estar False para poder usar "*"
    allow_methods=["*"],
    allow_headers=["*"],
)


# Endpoint oficial da VTEX
MD_BASE_URL = "https://www.mariadolores.com.br/api/catalog_system/pub/products/search/"

# 🔴 IMPORTANTE:
# Como você está numa rede corporativa que intercepta HTTPS,
# precisamos desabilitar a verificação do certificado para funcionar.
VERIFY_SSL = False  # em casa/pessoal você pode deixar True, se quiser


# ==============================================================
# Helpers de normalização / escolha de SKU e imagem
# ==============================================================

def normalizar_texto(s: Optional[str]) -> str:
    if not s:
        return ""
    s = s.strip().upper()
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    return s


def escolher_melhor_imagem(item: Dict[str, Any]) -> Optional[str]:
    imagens = item.get("images") or []
    if not imagens:
        return None

    # 1) tenta uma imagem "normal" (sem label ou label vazio)
    for img in imagens:
        label = (img.get("imageLabel") or "").strip()
        if not label:
            return img.get("imageUrl")

    # 2) se não tiver, pega a primeira mesmo
    return imagens[0].get("imageUrl")


def _listar_opcoes_sku_imagem(
    produtos: List[Dict[str, Any]],
    codigo: str,
    banho: Optional[str],
    pedra: Optional[str],
) -> List[Dict[str, Any]]:
    """
    Função interna que aplica a MESMA lógica de filtro usada em /md/sku-image-options
    e retorna uma lista de opções de imagem para (código, banho, pedra).
    """
    codigo_norm = normalizar_texto(codigo)
    banho_norm = normalizar_texto(banho) if banho else ""
    pedra_norm = normalizar_texto(pedra) if pedra else ""

    opcoes: List[Dict[str, Any]] = []

    for prod in produtos:
        prod_ref = prod.get("productReference") or prod.get("productReferenceCode") or ""
        prod_ref_norm = normalizar_texto(prod_ref)

        # filtro básico por código (começando com MD2116, etc.)
        if "." in codigo_norm:
            if prod_ref_norm != codigo_norm:
                continue
        else:
            if not prod_ref_norm.startswith(codigo_norm):
                continue

        pedras = prod.get("Pedras") or []
        pedras_norm = [normalizar_texto(p) for p in pedras]
        pedra_label = pedras[0] if pedras else None

        for item in prod.get("items", []):
            banhos = item.get("Banho") or []
            banhos_norm = [normalizar_texto(b) for b in banhos]
            banho_label = banhos[0] if banhos else None

            # filtros "semânticos"
            if banho_norm:
                if not any(banho_norm in b for b in banhos_norm):
                    continue

            if pedra_norm:
                if not any(pedra_norm in p for p in pedras_norm):
                    continue

            image_url = escolher_melhor_imagem(item)
            if not image_url:
                continue

            opcoes.append(
                {
                    "codigo": prod_ref,
                    "banho": banho_label,
                    "pedra": pedra_label,
                    "image_url": image_url,
                }
            )

    return opcoes


def escolher_sku(
    produtos: List[Dict[str, Any]],
    codigo: str,
    banho: Optional[str],
    pedra: Optional[str],
) -> Optional[Dict[str, Any]]:
    """
    ⚠ Hoje essa função não é usada em nenhum endpoint.
      Mantive por compatibilidade, caso você esteja importando em outro lugar.
    """
    codigo_norm = normalizar_texto(codigo)
    banho_norm = normalizar_texto(banho) if banho else ""
    pedra_norm = normalizar_texto(pedra) if pedra else ""

    skus = []
    for prod in produtos:
        prod_ref = prod.get("productReference") or prod.get("productReferenceCode") or ""
        prod_ref_norm = normalizar_texto(prod_ref)

        pedras = prod.get("Pedras") or []
        pedras_norm = [normalizar_texto(p) for p in pedras]

        for item in prod.get("items", []):
            banhos = item.get("Banho") or []
            banhos_norm = [normalizar_texto(b) for b in banhos]

            skus.append(
                {
                    "produto": prod,
                    "item": item,
                    "codigo_norm": prod_ref_norm,
                    "banhos_norm": banhos_norm,
                    "pedras_norm": pedras_norm,
                }
            )

    if not skus:
        return None

    # 2) filtro por código (igual ou começa com)
    if "." in codigo_norm:
        candidatos = [s for s in skus if s["codigo_norm"] == codigo_norm]
    else:
        candidatos = [s for s in skus if s["codigo_norm"].startswith(codigo_norm)]

    if not candidatos:
        return None

    # 3) filtro por banho (se informado)
    if banho_norm:
        cand_banho = [
            s
            for s in candidatos
            if any(banho_norm == b or banho_norm in b for b in s["banhos_norm"])
        ]
        if cand_banho:
            candidatos = cand_banho

    # 4) filtro por pedra (se informada)
    if pedra_norm:
        cand_pedra = []
        for s in candidatos:
            pedras_norm = s["pedras_norm"]
            if any(
                pedra_norm == p or pedra_norm in p or p in pedra_norm
                for p in pedras_norm
            ):
                cand_pedra.append(s)
        if cand_pedra:
            candidatos = cand_pedra

    return candidatos[0] if candidatos else None


def buscar_imagem_por_codigo_pedra_banho(
    codigo: str,
    banho: Optional[str],
    pedra: Optional[str],
) -> Optional[str]:
    """
    Busca na VTEX pelo código e retorna a URL da imagem
    do SKU cuja combinação (código / pedra / banho) bate.
    Essa função NÃO lança HTTPException, só retorna None em caso de não encontrado.

    Agora usa a mesma lógica de filtro do endpoint /md/sku-image-options
    (via _listar_opcoes_sku_imagem).
    """
    try:
        resp = requests.get(
            f"{MD_BASE_URL}{codigo}",
            verify=VERIFY_SSL,
            timeout=10,
        )
        resp.raise_for_status()
    except requests.RequestException:
        return None

    produtos = resp.json()
    if not isinstance(produtos, list) or not produtos:
        return None

    opcoes = _listar_opcoes_sku_imagem(produtos, codigo, banho, pedra)
    if not opcoes:
        return None

    # devolve só a URL da primeira opção encontrada
    return opcoes[0]["image_url"]


# ==============================================================
# Enriquecimento de produto
# ==============================================================

def enriquecer_produto(prod: Dict[str, Any]) -> Dict[str, Any]:
    """
    A partir do JSON original da VTEX, extrai:
    - colecao_principal (primeira de 'Coleções')
    - imagem_principal (primeira imagem do primeiro item)
    - preco, preco_lista, preco_sem_desconto (Price, ListPrice, PriceWithoutDiscount)
    - percentual_desconto = (1 - preco/preco_lista)*100
    E adiciona isso diretamente no dicionário do produto.
    """

    # Coleção
    colecao = None
    colecoes = prod.get("Coleções")
    if isinstance(colecoes, list) and len(colecoes) > 0:
        colecao = colecoes[0]

    # Imagem e preços
    imagem = None
    preco = None
    preco_lista = None
    preco_sem_desc = None
    percentual_desconto = None

    items: List[Dict[str, Any]] = prod.get("items") or []
    if items:
        item0 = items[0]

        # Imagem principal
        imagens = item0.get("images") or []
        if imagens:
            imagem = imagens[0].get("imageUrl")

        # Preços (seller principal)
        sellers = item0.get("sellers") or []
        if sellers:
            offer = (sellers[0] or {}).get("commertialOffer") or {}
            preco = offer.get("Price")
            preco_lista = offer.get("ListPrice")
            preco_sem_desc = offer.get("PriceWithoutDiscount")

            try:
                if preco is not None and preco_lista and preco_lista > 0:
                    percentual_desconto = (1 - (preco / preco_lista)) * 100
            except Exception:
                percentual_desconto = None

    resumo = {
        "colecao_principal": colecao,
        "imagem_principal": imagem,
        "preco": preco,
        "preco_lista": preco_lista,
        "preco_sem_desconto": preco_sem_desc,
        "percentual_desconto": percentual_desconto,
    }

    prod["colecao_principal"] = colecao
    prod["imagem_principal"] = imagem
    prod["preco"] = preco
    prod["preco_lista"] = preco_lista
    prod["preco_sem_desconto"] = preco_sem_desc
    prod["percentual_desconto"] = percentual_desconto
    prod["md_resumo"] = resumo

    return prod


# ==============================================================
# Endpoints
# ==============================================================

@app.get("/md/search")
def search_md(
    ft: Optional[str] = Query(
        default=None,
        description="Texto de busca (mesmo campo 'ft' usado no site / VTEX)",
    ),
    productId: Optional[str] = Query(
        default=None,
        description="Filtrar por productId específico (opcional)",
    ),
):
    params: Dict[str, Any] = {}

    if ft:
        params["ft"] = ft
    if productId:
        params["productId"] = productId

    try:
        resp = requests.get(
            MD_BASE_URL,
            params=params,
            timeout=20,
            verify=VERIFY_SSL,
        )
        resp.raise_for_status()
    except requests.exceptions.SSLError as e:
        raise HTTPException(
            status_code=502,
            detail=(
                "Erro SSL ao chamar API Maria Dolores "
                "(provavelmente certificado da rede corporativa). "
                f"Detalhe técnico: {e}"
            ),
        )
    except requests.RequestException as e:
        raise HTTPException(
            status_code=502,
            detail=f"Erro ao chamar API Maria Dolores: {e}",
        )

    dados = resp.json()

    if isinstance(dados, list):
        dados = [enriquecer_produto(p) for p in dados]
    else:
        dados = enriquecer_produto(dados)

    return JSONResponse(content=dados)


@app.get("/image-proxy")
def image_proxy(
    url: str = Query(..., description="URL absoluta da imagem na VTEX"),
):
    try:
        r = requests.get(url, stream=True, timeout=20, verify=VERIFY_SSL)
        r.raise_for_status()
    except requests.exceptions.SSLError as e:
        raise HTTPException(
            status_code=502,
            detail=(
                "Erro SSL ao baixar imagem da VTEX "
                "(provavelmente certificado da rede corporativa). "
                f"Detalhe técnico: {e}"
            ),
        )
    except requests.RequestException as e:
        raise HTTPException(
            status_code=502,
            detail=f"Erro ao baixar imagem da VTEX: {e}",
        )

    content_type = r.headers.get("Content-Type", "image/jpeg")
    return StreamingResponse(r.raw, media_type=content_type)


@app.get("/md/sku-image-options")
def sku_image_options(
    codigo: str = Query(..., description="Código base, ex: 'MD2116' ou 'MD2116.FO.907'"),
    banho: Optional[str] = Query(None, description="Banho, pode ser parcial, ex: 'ouro'"),
    pedra: Optional[str] = Query(None, description="Pedra, pode ser parcial, ex: 'ágata'"),
):
    """
    Retorna as combinações possíveis de (código / banho / pedra) para um determinado código base,
    já com:
      - imagem (VTEX e proxied)
      - colecao_principal
      - preços (preco, preco_lista, preco_sem_desconto, percentual_desconto)
      - link do produto

    Esse endpoint é o "resumido" para uso no Base44 / frontend.
    """

    # 1) chama VTEX
    try:
        resp = requests.get(
            f"{MD_BASE_URL}{codigo}",
            timeout=20,
            verify=VERIFY_SSL,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Erro ao consultar VTEX: {e}")

    produtos = resp.json()
    if not isinstance(produtos, list) or not produtos:
        raise HTTPException(status_code=404, detail="Nenhum produto encontrado")

    # normalizações de busca
    codigo_norm = normalizar_texto(codigo)
    banho_norm = normalizar_texto(banho) if banho else ""
    pedra_norm = normalizar_texto(pedra) if pedra else ""

    opcoes = []

    for prod in produtos:
        # 🔹 já enriquecemos aqui (coleção, preços, imagem_principal, etc.)
        prod = enriquecer_produto(prod)

        prod_ref = prod.get("productReference") or prod.get("productReferenceCode") or ""
        prod_ref_norm = normalizar_texto(prod_ref)

        # filtro básico por código (igual se vier com ponto, ou começa com se vier só MD2116)
        if "." in codigo_norm:
            if prod_ref_norm != codigo_norm:
                continue
        else:
            if not prod_ref_norm.startswith(codigo_norm):
                continue

        pedras = prod.get("Pedras") or []
        pedras_norm = [normalizar_texto(p) for p in pedras]
        pedra_label = pedras[0] if pedras else None

        for item in prod.get("items", []):
            banhos = item.get("Banho") or []
            banhos_norm = [normalizar_texto(b) for b in banhos]
            banho_label = banhos[0] if banhos else None

            # filtros "semânticos"
            if banho_norm:
                if not any(banho_norm in b for b in banhos_norm):
                    continue

            if pedra_norm:
                if not any(pedra_norm in p for p in pedras_norm):
                    continue

            image_url = escolher_melhor_imagem(item)
            if not image_url:
                continue

            # monta a URL proxied usando o próprio backend
            proxied_path = f"/image-proxy?url={quote(image_url, safe='')}"
            proxied_url = proxied_path  # no front você prefixa com o host do backend

            # 🔹 aqui montamos o "dicionário resumido" para cada opção
            opcoes.append({
                "productId": prod.get("productId"),
                "codigo_completo": prod_ref,  # ex: MD2116.FO.970
                "codigo_busca": codigo,        # o que o usuário mandou
                "banho": banho_label,
                "pedra": pedra_label,

                # infos de produto / coleção
                "nome": prod.get("productName"),
                "colecao_principal": prod.get("colecao_principal"),
                "link": prod.get("link"),

                # preços já enriquecidos
                "preco": prod.get("preco"),
                "preco_lista": prod.get("preco_lista"),
                "preco_sem_desconto": prod.get("preco_sem_desconto"),
                "percentual_desconto": prod.get("percentual_desconto"),

                # imagens
                "imagem_principal": prod.get("imagem_principal"),  # do enriquecimento
                "image_url": image_url,        # desse SKU específico
                "proxied_url": proxied_url,    # passando pelo seu backend
            })

    if not opcoes:
        raise HTTPException(
            status_code=404,
            detail="Nenhuma combinação de imagem encontrada para esses filtros",
        )

    return opcoes

