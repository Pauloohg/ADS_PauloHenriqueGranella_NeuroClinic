from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.templating import templates
from app.db.session import get_db
from app.models.usuario import TipoUsuario, Usuario
from app.routers.auth import agora_local, get_usuario_por_tipo
from app.services import agenda_service

somente_funcionario = get_usuario_por_tipo(TipoUsuario.FUNCIONARIO)

router = APIRouter(prefix="/agenda", tags=["agenda"], dependencies=[Depends(somente_funcionario)])

TEMPLATE_AGENDA = "funcionario/agenda.html"


@router.get("")
def agenda(
    request: Request,
    visao: str | None = None,
    data: str | None = None,
    inicio: str | None = None,
    fim: str | None = None,
    usuario: Usuario = Depends(somente_funcionario),
    db: Session = Depends(get_db),
):
    hoje = agora_local().date()
    escolhida, erro = agenda_service.montar_visao(visao, data, inicio, fim, hoje)
    consultas = agenda_service.consultas_do_profissional(db, usuario.id, escolhida.inicio, escolhida.fim, hoje)
    contexto = {
        "usuario": usuario,
        "visao": escolhida,
        "hoje": hoje,
        "dias": agenda_service.agrupar_dias_vazios(agenda_service.agrupar_por_dia(escolhida, consultas), hoje),
        "total": len(consultas),
        "navegacao": agenda_service.navegacao(escolhida, hoje),
        "erro": erro,
        "erro_no_periodo": erro is not None and visao == agenda_service.PERIODO,
        "form": {"data": data or "", "inicio": inicio or "", "fim": fim or ""},
    }
    return templates.TemplateResponse(request, TEMPLATE_AGENDA, contexto)
