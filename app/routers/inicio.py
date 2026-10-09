from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.templating import templates
from app.db.session import get_db
from app.models.usuario import TipoUsuario, Usuario
from app.routers.auth import agora_local, get_usuario_opcional
from app.services import inicio_service

router = APIRouter(tags=["inicio"])

TEMPLATES_POR_TIPO = {
    TipoUsuario.CLIENTE: "inicio/cliente.html",
    TipoUsuario.FUNCIONARIO: "inicio/funcionario.html",
    TipoUsuario.ADMIN: "inicio/admin.html",
}


def _contexto_cliente(db: Session, usuario: Usuario, agora) -> dict:
    proxima = inicio_service.proxima_sessao(db, usuario.id, agora)
    return {
        "proxima_sessao": proxima,
        "rotulo_proxima_sessao": inicio_service.rotulo_dia(proxima.data_hora.date(), agora.date()) if proxima else None,
        "planos": inicio_service.planos_do_cliente(db, usuario.id, agora),
    }


def _contexto_funcionario(db: Session, usuario: Usuario, agora) -> dict:
    return {"especialidade": inicio_service.nome_especialidade_do_funcionario(db, usuario.id)}


def _contexto_admin(db: Session, usuario: Usuario, agora) -> dict:
    return {
        "total_funcionarios_ativos": inicio_service.contar_funcionarios_ativos(db),
        "total_especialidades": inicio_service.contar_especialidades(db),
    }


CONTEXTOS_POR_TIPO = {
    TipoUsuario.CLIENTE: _contexto_cliente,
    TipoUsuario.FUNCIONARIO: _contexto_funcionario,
    TipoUsuario.ADMIN: _contexto_admin,
}


@router.get("/")
def inicio(request: Request, usuario: Usuario | None = Depends(get_usuario_opcional), db: Session = Depends(get_db)):
    if usuario is None:
        return templates.TemplateResponse(request, "index.html", {"usuario": None})

    agora = agora_local()
    contexto = {
        "usuario": usuario,
        "saudacao": inicio_service.saudacao(agora),
        **CONTEXTOS_POR_TIPO[usuario.tipo](db, usuario, agora),
    }
    return templates.TemplateResponse(request, TEMPLATES_POR_TIPO[usuario.tipo], contexto)
