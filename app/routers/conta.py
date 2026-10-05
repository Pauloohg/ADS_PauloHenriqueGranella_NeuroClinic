from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import RedirectResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.core.templating import templates
from app.db.session import get_db
from app.models.usuario import Usuario
from app.routers.auth import erros_por_campo, get_usuario_atual
from app.schemas.usuario import AlteracaoSenha
from app.services import auth_service

router = APIRouter(prefix="/conta", tags=["conta"])

TEMPLATE_ALTERAR_SENHA = "conta/alterar_senha.html"
MSG_SENHA_ATUAL_INCORRETA = "A senha atual está incorreta."


@router.get("/alterar-senha")
def alterar_senha_form(request: Request, usuario: Usuario = Depends(get_usuario_atual)):
    contexto = {"usuario": usuario, "erros": {}, "sucesso": request.query_params.get("ok") == "1"}
    return templates.TemplateResponse(request, TEMPLATE_ALTERAR_SENHA, contexto)


@router.post("/alterar-senha")
def alterar_senha(
    request: Request,
    senha_atual: Annotated[str, Form()] = "",
    nova_senha: Annotated[str, Form()] = "",
    confirmacao_senha: Annotated[str, Form()] = "",
    usuario: Usuario = Depends(get_usuario_atual),
    db: Session = Depends(get_db),
):
    try:
        dados = AlteracaoSenha(senha_atual=senha_atual, nova_senha=nova_senha, confirmacao_senha=confirmacao_senha)
        auth_service.alterar_senha(db, usuario, dados)
    except ValidationError as exc:
        erros = erros_por_campo(exc.errors())
    except auth_service.SenhaAtualIncorreta:
        erros = {"senha_atual": MSG_SENHA_ATUAL_INCORRETA}
    else:
        return RedirectResponse("/conta/alterar-senha?ok=1", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(
        request, TEMPLATE_ALTERAR_SENHA, {"usuario": usuario, "erros": erros},
        status_code=status.HTTP_400_BAD_REQUEST,
    )
