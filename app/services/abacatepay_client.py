import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

import httpx

from app.core.config import ABACATEPAY_TIMEOUT_SEGUNDOS, settings

logger = logging.getLogger(__name__)

PIX = "PIX"
BOLETO = "BOLETO"
LIMITE_TEXTO = 500
PREFIXO_IMAGEM = "data:image/png;base64,"
ERRO_CARACTERE = "disallowed character"

MSG_INDISPONIVEL = "Não foi possível falar com o serviço de pagamento agora. Tente novamente em alguns minutos."
MSG_CONFIGURACAO = "O pagamento on-line está indisponível no momento. Avise a clínica."
MSG_RECUSADA = "O serviço de pagamento não aceitou gerar a cobrança. Tente novamente ou avise a clínica."

TRADUCAO_PONTUACAO = str.maketrans({
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-", "−": "-",
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
    "“": "'", "”": "'", "„": "'", "‟": "'", "″": "'", "«": "'", "»": "'",
})
_EMAIL = re.compile(r"[^\s'\"<>()]+@[^\s'\"<>()]+")
_DOCUMENTO = re.compile(r"\d[\d.\-/]{9,}\d")

# Nos testes vira um httpx.MockTransport; None usa a rede de verdade
_TRANSPORTE: httpx.BaseTransport | None = None


class ErroAbacatePay(Exception):
    def __init__(self, mensagem: str):
        super().__init__(mensagem)
        self.mensagem = mensagem


class CaractereRecusado(ErroAbacatePay):
    pass


@dataclass(frozen=True)
class Cobranca:
    id: str
    status: str
    valor_centavos: int
    dev_mode: bool
    br_code: str | None
    br_code_base64: str | None
    linha_digitavel: str | None
    codigo_barras: str | None
    url_boleto: str | None


def centavos(valor: Decimal) -> int:
    return int((valor * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def sanear_texto(texto: str | None, limite: int = LIMITE_TEXTO) -> str:
    texto = unicodedata.normalize("NFKD", (texto or "").translate(TRADUCAO_PONTUACAO))
    texto = "".join(c if c.isprintable() else " " for c in texto if not unicodedata.combining(c))
    texto = texto.encode("ascii", "ignore").decode("ascii")
    return " ".join(texto.split())[:limite].rstrip()


def formatar_linha_digitavel(numero: str | None) -> str | None:
    digitos = "".join(c for c in numero or "" if c.isdigit())
    if len(digitos) != 47:
        return numero or None
    d = digitos
    return f"{d[:5]}.{d[5:10]} {d[10:15]}.{d[15:21]} {d[21:26]}.{d[26:32]} {d[32]} {d[33:]}"


def _imagem_qr_code(valor: str | None) -> str | None:
    if not valor:
        return None
    return valor if valor.startswith("data:") else PREFIXO_IMAGEM + valor


def _mascarar(texto: str) -> str:
    if settings.ABACATEPAY_API_KEY:
        texto = texto.replace(settings.ABACATEPAY_API_KEY, "[chave]")
    return _DOCUMENTO.sub("[documento]", _EMAIL.sub("[e-mail]", texto))


def _requisicao(metodo: str, caminho: str, params: dict | None = None, corpo: dict | None = None) -> dict:
    if not settings.ABACATEPAY_API_KEY:
        logger.error("AbacatePay: ABACATEPAY_API_KEY não configurada")
        raise ErroAbacatePay(MSG_CONFIGURACAO)
    try:
        with httpx.Client(
            base_url=settings.ABACATEPAY_BASE_URL.rstrip("/"),
            headers={"Authorization": f"Bearer {settings.ABACATEPAY_API_KEY}"},
            timeout=ABACATEPAY_TIMEOUT_SEGUNDOS,
            transport=_TRANSPORTE,
        ) as cliente:
            resposta = cliente.request(metodo, caminho, params=params, json=corpo)
    except httpx.TimeoutException:
        logger.warning("AbacatePay: tempo esgotado em %s", caminho)
        raise ErroAbacatePay(MSG_INDISPONIVEL) from None
    except httpx.HTTPError as exc:
        # Só o tipo do erro: a mensagem de algumas exceções do httpx inclui a requisição
        logger.warning("AbacatePay: erro de rede em %s (%s)", caminho, type(exc).__name__)
        raise ErroAbacatePay(MSG_INDISPONIVEL) from None

    try:
        conteudo = resposta.json()
    except ValueError:
        conteudo = None
    erro = conteudo.get("error") if isinstance(conteudo, dict) else None
    if resposta.status_code < 400 and isinstance(conteudo, dict) and not erro and "data" in conteudo:
        return conteudo["data"]

    # Nunca registrar o corpo enviado: tem CPF e e-mail do pagador; só o status e o "error" devolvido, mascarado
    detalhe = _mascarar(str(erro if erro is not None else resposta.text[:300]))
    logger.warning("AbacatePay: falha em %s (HTTP %s): %s", caminho, resposta.status_code, detalhe)
    if resposta.status_code in (401, 403):
        raise ErroAbacatePay(MSG_CONFIGURACAO)
    if resposta.status_code >= 500:
        raise ErroAbacatePay(MSG_INDISPONIVEL)
    if ERRO_CARACTERE in str(erro or "").lower():
        raise CaractereRecusado(MSG_RECUSADA)
    raise ErroAbacatePay(MSG_RECUSADA)


def _ler_cobranca(dados: dict) -> Cobranca:
    if not isinstance(dados, dict) or not dados.get("id"):
        logger.error("AbacatePay: resposta sem id da cobrança")
        raise ErroAbacatePay(MSG_RECUSADA)
    # A documentação diz que barCode é a linha digitável, mas a API real devolve a linha em identificationNumber
    boleto = dados["id"].startswith("bole_") or bool(dados.get("identificationNumber") or dados.get("barCode"))
    return Cobranca(
        id=dados["id"],
        status=dados.get("status") or "",
        valor_centavos=int(dados.get("amount") or 0),
        dev_mode=dados.get("devMode") is True,
        br_code=None if boleto else dados.get("brCode"),
        br_code_base64=None if boleto else _imagem_qr_code(dados.get("brCodeBase64")),
        linha_digitavel=formatar_linha_digitavel(dados.get("identificationNumber")) if boleto else None,
        codigo_barras=dados.get("barCode") if boleto else None,
        url_boleto=dados.get("url") if boleto else None,
    )


def _criar(metodo: str, dados: dict) -> Cobranca:
    return _ler_cobranca(_requisicao("POST", "/transparents/create", corpo={"method": metodo, "data": dados}))


def criar_cobranca(
    metodo: str,
    valor_centavos: int,
    pagador: dict,
    plano_id: int,
    descricao: str,
    expira_em_segundos: int | None = None,
    vencimento: date | None = None,
) -> Cobranca:
    dados = {
        "amount": valor_centavos,
        "description": sanear_texto(descricao),
        "customer": pagador,
        "externalId": f"plano-{plano_id}",
        "metadata": {"plano_id": plano_id},
    }
    if metodo == PIX:
        dados["expiresIn"] = expira_em_segundos
    else:
        dados["dueDate"] = vencimento.isoformat()
    try:
        return _criar(metodo, dados)
    except CaractereRecusado:
        logger.warning("AbacatePay: caractere recusado; repetindo uma vez com os textos em ASCII")
    dados["customer"] = {**pagador, "name": sanear_texto(pagador.get("name"))}
    try:
        return _criar(metodo, dados)
    except CaractereRecusado:
        raise ErroAbacatePay(MSG_RECUSADA) from None


def consultar_cobranca(cobranca_id: str) -> Cobranca:
    lista = _requisicao("GET", "/transparents/list", params={"id": cobranca_id})
    encontrada = next((item for item in lista or [] if isinstance(item, dict) and item.get("id") == cobranca_id), None)
    if encontrada is None:
        logger.error("AbacatePay: cobrança %s não encontrada na consulta", cobranca_id)
        raise ErroAbacatePay(MSG_INDISPONIVEL)
    return _ler_cobranca(encontrada)


def simular_pagamento(cobranca_id: str) -> Cobranca:
    return _ler_cobranca(_requisicao("POST", "/transparents/simulate-payment", params={"id": cobranca_id}))
