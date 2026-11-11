from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.templating import templates
from app.db.session import get_db
from app.models.usuario import TipoUsuario, Usuario
from app.routers.auth import agora_local, get_usuario_opcional
from app.services import inicio_profissional_service, inicio_service, paciente_service, painel_service, plano_service

router = APIRouter(tags=["inicio"])

TEMPLATES_POR_TIPO = {
    TipoUsuario.CLIENTE: "inicio/cliente.html",
    TipoUsuario.FUNCIONARIO: "inicio/funcionario.html",
    TipoUsuario.ADMIN: "inicio/admin.html",
}


def _contexto_cliente(db: Session, usuario: Usuario, agora) -> dict:
    plano_service.remover_planos_vencidos(db, agora)
    proxima = inicio_service.proxima_sessao(db, usuario.id, agora)
    planos = inicio_service.planos_do_cliente(db, usuario.id, agora)
    tem_atendido = bool(paciente_service.listar_do_cliente(db, usuario.id))
    return {
        "proxima_sessao": proxima,
        "quando_proxima_sessao": inicio_service.quando_sessao(proxima.data_hora, agora) if proxima else None,
        "planos": planos,
        "primeiros_passos": inicio_service.primeiros_passos(tem_atendido, planos),
    }


def _contexto_funcionario(db: Session, usuario: Usuario, agora) -> dict:
    return {"inicio": inicio_profissional_service.montar_inicio(db, usuario.id, agora)}


def _contexto_admin(db: Session, usuario: Usuario, agora) -> dict:
    mes, _ = painel_service.resolver_mes(None, agora)
    cadastros = inicio_service.resumo_dos_cadastros(db, agora)
    return {
        "titulo_do_mes": inicio_service.titulo_do_mes(mes),
        "mes": painel_service.resumo_do_mes(db, mes),
        "cadastros": [
            ("/admin/clientes", "bi-people", "Clientes", inicio_service.texto_clientes(cadastros)),
            ("/admin/funcionarios", "bi-person-badge", "Profissionais", inicio_service.texto_profissionais(cadastros)),
            ("/admin/especialidades", "bi-tags", "Especialidades", inicio_service.texto_especialidades(cadastros)),
        ],
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
