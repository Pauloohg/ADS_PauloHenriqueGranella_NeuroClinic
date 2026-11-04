import logging

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.usuario import Usuario
from app.routers.auth import get_usuario_atual
from app.services import avaliacao_service
from app.services.avaliacao_service import AvaliacaoNaoEncontrada

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/avaliacoes", tags=["avaliacoes"])


def _nao_encontrada() -> HTTPException:
    # RN03: sem permissão também é 404, para não confirmar que a avaliação existe
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Avaliação não encontrada.")


@router.get("/{avaliacao_id}/arquivo")
def baixar_arquivo(avaliacao_id: int, usuario: Usuario = Depends(get_usuario_atual), db: Session = Depends(get_db)):
    try:
        arquivo = avaliacao_service.buscar_arquivo(db, usuario, avaliacao_id)
        caminho = avaliacao_service.caminho_do_arquivo(arquivo.nome)
    except AvaliacaoNaoEncontrada:
        raise _nao_encontrada() from None
    if not caminho.is_file():
        logger.warning("Arquivo da avaliação %s não encontrado no disco", avaliacao_id)
        raise _nao_encontrada()
    return FileResponse(
        caminho,
        media_type="application/pdf",
        filename=f"avaliacao-{arquivo.data_sessao.isoformat()}.pdf",
        content_disposition_type="inline",
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )
