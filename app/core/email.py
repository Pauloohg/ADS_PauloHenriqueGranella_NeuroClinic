import logging

from fastapi_mail import ConnectionConfig, FastMail, MessageSchema, MessageType

from app.core.config import settings

logger = logging.getLogger(__name__)


def _configuracao() -> ConnectionConfig:
    return ConnectionConfig(
        MAIL_USERNAME=settings.MAIL_USERNAME,
        MAIL_PASSWORD=settings.MAIL_PASSWORD,
        MAIL_FROM=settings.MAIL_FROM,
        MAIL_FROM_NAME=settings.APP_NAME,
        MAIL_SERVER=settings.MAIL_SERVER,
        MAIL_PORT=settings.MAIL_PORT,
        MAIL_STARTTLS=True,
        MAIL_SSL_TLS=False,
        USE_CREDENTIALS=bool(settings.MAIL_USERNAME),
        VALIDATE_CERTS=True,
        TIMEOUT=15,
    )


async def enviar_email(destinatario: str, assunto: str, corpo_html: str) -> bool:
    try:
        mensagem = MessageSchema(
            subject=assunto, recipients=[destinatario], body=corpo_html, subtype=MessageType.html
        )
        await FastMail(_configuracao()).send_message(mensagem)
    except Exception:
        logger.exception("Falha ao enviar e-mail para %s (assunto: %s)", destinatario, assunto)
        return False
    return True
