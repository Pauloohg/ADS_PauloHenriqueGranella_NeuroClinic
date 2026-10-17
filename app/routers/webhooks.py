import json
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.routers.auth import agora_local, agora_utc
from app.services import pagamento_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webhooks"])


def _resposta(codigo: int, mensagem: str) -> JSONResponse:
    return JSONResponse({"mensagem": mensagem}, status_code=codigo)


@router.post("/webhooks/abacatepay")
async def webhook_abacatepay(request: Request, tarefas: BackgroundTasks, db: Session = Depends(get_db)):
    if not pagamento_service.secret_valido(request.query_params.get("webhookSecret")):
        return _resposta(status.HTTP_401_UNAUTHORIZED, "não autorizado")
    # A assinatura é calculada sobre os bytes exatamente como chegaram: nunca reserializar o JSON antes de validar
    corpo_bruto = await request.body()
    if not pagamento_service.assinatura_valida(corpo_bruto, request.headers.get("X-Webhook-Signature")):
        return _resposta(status.HTTP_401_UNAUTHORIZED, "assinatura inválida")
    try:
        evento = json.loads(corpo_bruto)
    except ValueError:
        return _resposta(status.HTTP_400_BAD_REQUEST, "corpo inválido")
    if not isinstance(evento, dict):
        return _resposta(status.HTTP_400_BAD_REQUEST, "corpo inválido")

    try:
        resultado = pagamento_service.processar_evento(
            db, evento, agora_local(), agora_utc().replace(tzinfo=None), tarefas
        )
    except Exception:
        db.rollback()
        logger.exception("Webhook AbacatePay: erro ao processar o evento %s", evento.get("id"))
        return _resposta(status.HTTP_500_INTERNAL_SERVER_ERROR, "erro ao processar")
    return _resposta(status.HTTP_200_OK, resultado.situacao)
