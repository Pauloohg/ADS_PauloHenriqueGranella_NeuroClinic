from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.templating import templates
from app.db.session import get_db
from app.models.usuario import TipoUsuario, Usuario
from app.routers.auth import agora_local, get_usuario_por_tipo
from app.services import consultas_service, paciente_service, plano_service
from app.services.admin_clientes_service import interpretar_pagina

somente_cliente = get_usuario_por_tipo(TipoUsuario.CLIENTE)

router = APIRouter(prefix="/historico", tags=["historico"], dependencies=[Depends(somente_cliente)])

TEMPLATE_HISTORICO = "cliente/historico.html"


def _atendido_escolhido(atendidos: list, atendido: str | None):
    if atendido is None:
        return atendidos[0] if atendidos else None
    # Atendido de outro Cliente ou inexistente responde igual (404), para não revelar que o id existe
    escolhido = next((a for a in atendidos if str(a.id) == atendido.strip()), None)
    if escolhido is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Atendido não encontrado.")
    return escolhido


@router.get("")
def historico(
    request: Request,
    atendido: str | None = None,
    pagina: str | None = None,
    usuario: Usuario = Depends(somente_cliente),
    db: Session = Depends(get_db),
):
    agora = agora_local()
    # Antes de listar: a remoção faz commit e expiraria os atendidos já carregados
    plano_service.remover_planos_vencidos(db, agora)
    atendidos = paciente_service.listar_do_cliente(db, usuario.id)
    escolhido = _atendido_escolhido(atendidos, atendido)
    resultado = None
    if escolhido is not None:
        resultado = consultas_service.historico_do_atendido(
            db, usuario.id, escolhido.id, agora, interpretar_pagina(pagina),
        )
    contexto = {
        "usuario": usuario,
        "atendidos": atendidos,
        "escolhido": escolhido,
        "resultado": resultado,
    }
    return templates.TemplateResponse(request, TEMPLATE_HISTORICO, contexto)
