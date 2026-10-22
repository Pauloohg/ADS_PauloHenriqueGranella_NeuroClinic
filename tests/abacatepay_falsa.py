import json
from datetime import date, datetime, time, timedelta

import httpx

CHAVE_API_TESTE = "chave-de-teste-nao-deve-aparecer-no-log"
SECRET_WEBHOOK_TESTE = "secret-do-webhook-de-teste"
QR_CODE_BASE64_PURO = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
QR_CODE_BASE64 = "data:image/png;base64," + QR_CODE_BASE64_PURO
BR_CODE = "00020101021226950014br.gov.bcb.pix2573pix.exemplo/qr/v3/at/teste5204000053039865802BR6304ABCD"
BR_CODE_ALTERNATIVO = "00020101021226950014br.gov.bcb.pix2573pix.exemplo/qr/v3/at/boleto5204000053039865802BR6304EF01"
IDENTIFICATION_NUMBER = "00190000090275928800021932978170187890000075000"  # 47 dígitos: linha digitável
LINHA_DIGITAVEL = "00190.00009 02759.288000 21932.978170 1 87890000075000"
CODIGO_BARRAS = "00191878900000750000000002759288002193297817"  # 44 dígitos
URL_BOLETO = "https://app.abacatepay.com/pay/bole_teste123/boleto"


def cobranca_pix(valor=75000, id_="pix_char_teste123", status="PENDING", dev_mode=True):
    return {
        "id": id_, "amount": valor, "status": status, "devMode": dev_mode, "brCode": BR_CODE,
        "brCodeBase64": QR_CODE_BASE64, "platformFee": 80, "receiptUrl": None,
        "createdAt": "2026-08-01T13:00:00.000Z", "updatedAt": "2026-08-01T13:00:00.000Z",
        "expiresAt": "2026-08-01T15:59:00.000Z", "metadata": {"plano_id": 1},
    }


# A API devolve o fim do dia do dueDate no horário de Brasília (UTC-3), em UTC: 31/07 -> 2026-08-01T02:59:59.999Z
def expires_at_do_boleto(due_date: str) -> str:
    fim_utc = datetime.combine(date.fromisoformat(due_date) + timedelta(days=1), time(2, 59, 59, 999000))
    return fim_utc.isoformat(timespec="milliseconds") + "Z"


def cobranca_boleto(valor=75000, id_="bole_teste123", status="PENDING", dev_mode=True):
    return {
        "id": id_, "amount": valor, "status": status, "devMode": dev_mode,
        "barCode": CODIGO_BARRAS, "identificationNumber": IDENTIFICATION_NUMBER, "url": URL_BOLETO,
        "brCode": BR_CODE_ALTERNATIVO, "brCodeBase64": QR_CODE_BASE64, "platformFee": 250, "receiptUrl": None,
        "createdAt": "2026-08-01T13:00:00.000Z", "updatedAt": "2026-08-01T13:00:00.000Z",
        "expiresAt": "2026-08-02T02:59:59.999Z", "metadata": {"plano_id": 1},
    }


def erro_caractere(campo: str, caractere: str) -> httpx.Response:
    mensagem = (
        f"Disallowed character in {campo}: '{caractere}' (U+{ord(caractere):04X}). Replace it with '-'."
    )
    return httpx.Response(400, json={"data": None, "error": mensagem, "success": False})


def _primeiro_nao_ascii(texto) -> str | None:
    return next((c for c in texto or "" if ord(c) > 127), None) if isinstance(texto, str) else None


class ApiFalsa:
    def __init__(self):
        self.requisicoes: list[httpx.Request] = []
        self.respostas: dict[str, object] = {}
        self.recusar_nome_nao_ascii = False

    def responder(self, caminho: str, resposta) -> None:
        self.respostas[caminho] = resposta

    def corpos(self, caminho: str) -> list[dict]:
        return [json.loads(r.content) for r in self.requisicoes if r.url.path.endswith(caminho)]

    def chamadas(self, caminho: str) -> list[httpx.Request]:
        return [r for r in self.requisicoes if r.url.path.endswith(caminho)]

    def _validar_textos(self, requisicao: httpx.Request) -> httpx.Response | None:
        dados = json.loads(requisicao.content).get("data", {})
        caractere = _primeiro_nao_ascii(dados.get("description"))
        if caractere:
            return erro_caractere("description", caractere)
        caractere = _primeiro_nao_ascii((dados.get("customer") or {}).get("name"))
        if caractere and self.recusar_nome_nao_ascii:
            return erro_caractere("customer.name", caractere)
        return None

    def _validar_vencimento(self, requisicao: httpx.Request) -> httpx.Response | None:
        corpo = json.loads(requisicao.content)
        if corpo.get("method") != "BOLETO":
            return None
        try:
            date.fromisoformat(corpo.get("data", {}).get("dueDate") or "")
        except (TypeError, ValueError):
            return httpx.Response(400, json={"data": None, "error": "Invalid dueDate", "success": False})
        return None

    def _com_vencimento(self, requisicao: httpx.Request, resposta: dict) -> dict:
        corpo = json.loads(requisicao.content)
        if corpo.get("method") != "BOLETO":
            return resposta
        return {**resposta, "expiresAt": expires_at_do_boleto(corpo["data"]["dueDate"])}

    def __call__(self, requisicao: httpx.Request) -> httpx.Response:
        self.requisicoes.append(requisicao)
        caminho = requisicao.url.path.removeprefix("/v2")
        if caminho == "/transparents/create":
            recusa = self._validar_textos(requisicao) or self._validar_vencimento(requisicao)
            if recusa is not None:
                return recusa
        resposta = self.respostas.get(caminho)
        if caminho == "/transparents/create" and isinstance(resposta, dict):
            resposta = self._com_vencimento(requisicao, resposta)
        if resposta is None:
            return httpx.Response(404, json={"error": "rota não configurada no teste"})
        if isinstance(resposta, Exception):
            raise resposta
        if isinstance(resposta, httpx.Response):
            return resposta
        return httpx.Response(200, json={"data": resposta, "error": None, "success": True})

    def transporte(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)
