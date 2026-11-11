from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.ids import inteiro_positivo, ler_opcao
from app.core.templating import templates
from app.db.session import get_db
from app.models.usuario import TipoUsuario, Usuario
from app.routers.auth import agora_local, get_usuario_por_tipo
from app.services import meus_atendidos_service
from app.services.admin_clientes_service import interpretar_pagina
from app.services.meus_atendidos_service import FILTRO_TODOS, FILTROS, AtendidoNaoEncontrado

somente_funcionario = get_usuario_por_tipo(TipoUsuario.FUNCIONARIO)

router = APIRouter(prefix="/meus-atendidos", tags=["meus-atendidos"], dependencies=[Depends(somente_funcionario)])

TEMPLATE_LISTA = "funcionario/meus_atendidos.html"
TEMPLATE_DETALHE = "funcionario/meu_atendido.html"
TAMANHO_MAXIMO_BUSCA = 100


@router.get("")
def lista(
    request: Request,
    q: str | None = None,
    pagina: str | None = None,
    filtro: str | None = None,
    usuario: Usuario = Depends(somente_funcionario),
    db: Session = Depends(get_db),
):
    resultado = meus_atendidos_service.atendidos_do_profissional(
        db, usuario.id, agora_local(), interpretar_pagina(pagina), (q or "")[:TAMANHO_MAXIMO_BUSCA],
        ler_opcao(filtro, FILTROS, FILTRO_TODOS),
    )
    return templates.TemplateResponse(request, TEMPLATE_LISTA, {"usuario": usuario, "resultado": resultado})


@router.get("/{paciente_id}")
def detalhe(
    request: Request,
    paciente_id: str,
    pagina: str | None = None,
    usuario: Usuario = Depends(somente_funcionario),
    db: Session = Depends(get_db),
):
    # Sem vínculo, inexistente ou id inválido respondem igual (404), para não revelar que o atendido existe
    try:
        numero = inteiro_positivo(paciente_id)
        if numero is None:
            raise AtendidoNaoEncontrado
        dados = meus_atendidos_service.detalhe_do_atendido(
            db, usuario.id, numero, agora_local(), interpretar_pagina(pagina),
        )
    except AtendidoNaoEncontrado:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Atendido não encontrado.") from None
    return templates.TemplateResponse(request, TEMPLATE_DETALHE, {"usuario": usuario, "detalhe": dados})
