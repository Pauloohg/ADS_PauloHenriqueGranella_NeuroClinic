import json
import sys
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select

import app.models  # noqa: F401 — registra todas as tabelas antes de usar a sessão
from app.core.config import settings
from app.db.session import SessionLocal
from app.models.pagamento import FormaPagamento, Pagamento, StatusPagamento
from app.models.plano import Plano
from app.services.abacatepay_client import centavos
from app.services.pagamento_service import EVENTO_PAGAMENTO_CONFIRMADO, calcular_assinatura

USO = "Uso: python -m scripts.simular_webhook <id do plano>"
URL_WEBHOOK_LOCAL = "http://localhost:8000/webhooks/abacatepay"
HOSTS_LOCAIS = {"localhost", "127.0.0.1", "::1"}
PREFIXO_CHAVE_DEV = "abc_dev_"
METODO_POR_FORMA = {FormaPagamento.PIX: "PIX", FormaPagamento.BOLETO: "BOLETO"}
TIMEOUT_SEGUNDOS = 10


def _instante(agora_utc: datetime) -> str:
    return agora_utc.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def montar_evento(pagamento: Pagamento, plano: Plano, agora_utc: datetime) -> dict:
    metodo = METODO_POR_FORMA[pagamento.forma_pagamento]
    valor = centavos(plano.valor_total)
    instante = _instante(agora_utc)
    # Dados do pagador são fictícios de propósito: a rota só lê data.transparent.id e o valor
    pagador = {"name": "Pagador simulado", "taxId": "***.***.***-**", "isSameAsCustomer": True}
    return {
        "id": f"log_simulado_{uuid.uuid4().hex}",
        "event": EVENTO_PAGAMENTO_CONFIRMADO,
        "apiVersion": 2,
        "devMode": True,
        "data": {
            "transparent": {
                "id": pagamento.abacatepay_payment_id, "externalId": f"plano-{plano.id}", "amount": valor,
                "paidAmount": valor, "platformFee": 0, "status": "PAID", "items": [], "methods": [metodo],
                "frequency": "ONE_TIME", "coupons": [], "devMode": True, "customerId": "cust_simulado",
                "receiptUrl": None, "metadata": {"plano_id": plano.id}, "createdAt": instante, "updatedAt": instante,
            },
            "customer": {"id": "cust_simulado", "name": "Pagador simulado", "email": "pagador@exemplo.com",
                         "taxId": "***.***.***-**"},
            "payerInformation": {
                "method": metodo,
                "utms": {"source": None, "medium": None, "campaign": None, "term": None, "content": None},
                metodo: pagador,
            },
        },
    }


def montar_envio(evento: dict, segredo: str) -> tuple[dict, bytes, dict]:
    corpo = json.dumps(evento, ensure_ascii=False).encode()
    cabecalhos = {"Content-Type": "application/json", "X-Webhook-Signature": calcular_assinatura(corpo)}
    return {"webhookSecret": segredo}, corpo, cabecalhos


def destino_local(url: str) -> bool:
    return urlsplit(url).hostname in HOSTS_LOCAIS


def _frase(resposta: httpx.Response) -> str:
    try:
        situacao = resposta.json().get("mensagem", "")
    except ValueError:
        situacao = ""
    if resposta.status_code == 200 and situacao == "confirmado":
        return "webhook aceito; pagamento confirmado e sessões agendadas."
    if resposta.status_code == 200:
        return f"webhook aceito; resultado: {situacao or 'sem detalhe'}."
    if resposta.status_code == 401:
        return "webhook recusado (segredo ou assinatura inválidos)."
    return "webhook não processado; veja o log do servidor."


def main(argumentos: list[str], sessao_banco=SessionLocal, cliente_http=None, url: str = URL_WEBHOOK_LOCAL) -> int:
    if len(argumentos) != 1 or not argumentos[0].strip().isdigit():
        print(USO)
        return 1
    if not settings.ABACATEPAY_API_KEY.startswith(PREFIXO_CHAVE_DEV):
        print("Recusado: a ABACATEPAY_API_KEY do .env não é de Dev mode (precisa começar com abc_dev_).")
        return 1
    if not destino_local(url):
        print("Recusado: o webhook simulado só pode ser enviado para localhost.")
        return 1
    if not settings.ABACATEPAY_WEBHOOK_SECRET:
        print("Configure ABACATEPAY_WEBHOOK_SECRET no .env antes de simular o webhook.")
        return 1

    plano_id = int(argumentos[0])
    with sessao_banco() as db:
        pagamento = db.scalar(select(Pagamento).where(Pagamento.plano_id == plano_id))
        plano = db.get(Plano, plano_id)
        if pagamento is None or plano is None or not pagamento.abacatepay_payment_id:
            print(f"O plano {plano_id} não tem cobrança gerada. Gere a cobrança pela tela de pagamento antes.")
            return 1
        if pagamento.status == StatusPagamento.CONFIRMADO:
            print(f"O pagamento do plano {plano_id} já está confirmado; nada foi enviado.")
            return 0
        evento = montar_evento(pagamento, plano, datetime.now(timezone.utc))

    params, corpo, cabecalhos = montar_envio(evento, settings.ABACATEPAY_WEBHOOK_SECRET)
    cliente = cliente_http or httpx.Client(timeout=TIMEOUT_SEGUNDOS)
    try:
        resposta = cliente.post(url, params=params, content=corpo, headers=cabecalhos)
    except httpx.ConnectError:
        print("O servidor não está rodando em localhost:8000. Inicie com 'uvicorn app.main:app --reload' e tente de novo.")
        return 1
    except httpx.HTTPError as exc:
        print(f"Falha ao enviar o webhook ({type(exc).__name__}).")
        return 1
    finally:
        if cliente_http is None:
            cliente.close()

    print(f"HTTP {resposta.status_code}: {_frase(resposta)}")
    return 0 if resposta.status_code == 200 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
