import logging
from datetime import datetime, timezone

from fastapi import BackgroundTasks
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import security
from app.core.config import settings
from app.core.email import enviar_email
from app.core.templating import templates
from app.models.cliente import Cliente
from app.models.usuario import TipoUsuario, Usuario
from app.schemas.usuario import (
    ClienteCadastro,
    ConfirmacaoRedefinicaoSenha,
    Login,
    SolicitacaoRedefinicaoSenha,
)

logger = logging.getLogger(__name__)

ASSUNTO_REDEFINICAO_SENHA = "NeuroClinic — redefinição de senha"


class EmailJaCadastrado(Exception):
    pass


# Não separar em "e-mail inexistente" e "senha errada" (HU01: não indicar qual dos dois falhou)
class CredenciaisInvalidas(Exception):
    pass


def buscar_por_email(db: Session, email: str) -> Usuario | None:
    return db.scalar(select(Usuario).where(Usuario.email == email.lower()))


def cadastrar_cliente(db: Session, dados: ClienteCadastro, agora: datetime) -> Usuario:
    if buscar_por_email(db, dados.email) is not None:
        raise EmailJaCadastrado

    usuario = Usuario(
        nome=dados.nome,
        email=dados.email,
        senha_hash=security.gerar_hash_senha(dados.senha),
        tipo=TipoUsuario.CLIENTE,
        criado_em=agora.astimezone(timezone.utc).replace(tzinfo=None),  # coluna sem fuso, em UTC
    )
    db.add(usuario)
    db.flush()  # não remover: Cliente precisa de usuario.id antes do commit
    db.add(Cliente(usuario_id=usuario.id, telefone=dados.telefone))
    db.commit()
    db.refresh(usuario)
    return usuario


def autenticar(db: Session, dados: Login, agora: datetime) -> str:
    usuario = buscar_por_email(db, dados.email)
    if usuario is None or not security.verificar_senha(dados.senha, usuario.senha_hash):
        raise CredenciaisInvalidas
    return security.criar_token_acesso(usuario.id, usuario.tipo.value, agora)


def solicitar_redefinicao_senha(
    db: Session,
    dados: SolicitacaoRedefinicaoSenha,
    url_base: str,
    agora: datetime,
    tarefas: BackgroundTasks | None = None,
) -> str | None:
    usuario = buscar_por_email(db, dados.email)
    if usuario is None:
        return None

    token = security.criar_token_redefinicao(usuario.id, usuario.senha_hash, agora)
    link = f"{url_base.rstrip('/')}/redefinir-senha/{token}"
    logger.info("Link de redefinição de senha para %s: %s", usuario.email, link)
    if tarefas is not None:
        corpo = templates.get_template("email/redefinicao_senha.html").render(
            nome=usuario.nome, link=link, validade_minutos=settings.RESET_TOKEN_EXPIRE_MINUTES
        )
        tarefas.add_task(enviar_email, usuario.email, ASSUNTO_REDEFINICAO_SENHA, corpo)
    return link


def validar_token_redefinicao(db: Session, token: str, agora: datetime) -> Usuario:
    payload = security.decodificar_token_redefinicao(token, agora)
    usuario = db.get(Usuario, int(payload["sub"]))
    if usuario is None or payload.get("fp") != security.impressao_senha(usuario.senha_hash):
        raise security.TokenInvalido
    return usuario


def redefinir_senha(db: Session, dados: ConfirmacaoRedefinicaoSenha, agora: datetime) -> Usuario:
    usuario = validar_token_redefinicao(db, dados.token, agora)
    usuario.senha_hash = security.gerar_hash_senha(dados.nova_senha)
    db.commit()
    return usuario
