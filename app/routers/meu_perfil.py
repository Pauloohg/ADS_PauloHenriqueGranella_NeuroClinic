from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import RedirectResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.core.templating import resumo_corrija, templates
from app.db.session import get_db
from app.models.usuario import TipoUsuario, Usuario
from app.routers.admin import ROTULOS_CAMPOS_FUNCIONARIO
from app.routers.auth import erros_por_campo, get_usuario_por_tipo
from app.schemas.funcionario import CAMPOS_CONTATO, ContatoProfissional
from app.services import funcionario_service

somente_funcionario = get_usuario_por_tipo(TipoUsuario.FUNCIONARIO)

router = APIRouter(prefix="/meu-perfil", tags=["meu-perfil"], dependencies=[Depends(somente_funcionario)])

TEMPLATE_PERFIL = "funcionario/perfil.html"
# Mesmos rótulos e mesma ordem da parte "Perfil que o cliente vê" do formulário do Admin
ROTULOS_PERFIL = {
    campo: ROTULOS_CAMPOS_FUNCIONARIO[campo]
    for campo in ("formacao", "registro_profissional", "apresentacao", "telefone_contato", "email_contato")
}


def _pagina(request: Request, usuario: Usuario, db: Session, dados: dict, erros: dict, codigo: int = 200, sucesso=False):
    # O registro é sempre o do usuário do token: não existe id na URL nem no formulário
    resumo = funcionario_service.buscar(db, usuario.id)
    contexto = {
        "usuario": usuario, "profissional": resumo, "dados": dados, "erros": erros, "rotulos": ROTULOS_PERFIL,
        "perfil": funcionario_service.perfil_publico(resumo.usuario, resumo.funcionario), "sucesso": sucesso,
        "titulo_erros": resumo_corrija(len(erros), "Nada foi salvo.") if erros else None,
    }
    return templates.TemplateResponse(request, TEMPLATE_PERFIL, contexto, status_code=codigo)


@router.get("")
def perfil(request: Request, usuario: Usuario = Depends(somente_funcionario), db: Session = Depends(get_db)):
    funcionario = funcionario_service.buscar(db, usuario.id).funcionario
    dados = {campo: getattr(funcionario, campo) or "" for campo in CAMPOS_CONTATO}
    return _pagina(request, usuario, db, dados, {}, sucesso=request.query_params.get("ok") == "1")


@router.post("")
def atualizar_perfil(
    request: Request,
    telefone_contato: Annotated[str, Form()] = "",
    email_contato: Annotated[str, Form()] = "",
    formacao: Annotated[str, Form()] = "",
    registro_profissional: Annotated[str, Form()] = "",
    apresentacao: Annotated[str, Form()] = "",
    usuario: Usuario = Depends(somente_funcionario),
    db: Session = Depends(get_db),
):
    # Só os 5 campos do perfil público: nome, e-mail de login, especialidade, ativo e senha vindos no corpo são ignorados
    dados_form = {
        "telefone_contato": telefone_contato, "email_contato": email_contato, "formacao": formacao,
        "registro_profissional": registro_profissional, "apresentacao": apresentacao,
    }
    try:
        funcionario_service.atualizar_contato(db, usuario.id, ContatoProfissional(**dados_form))
    except ValidationError as exc:
        return _pagina(request, usuario, db, dados_form, erros_por_campo(exc.errors()), status.HTTP_400_BAD_REQUEST)
    return RedirectResponse("/meu-perfil?ok=1", status_code=status.HTTP_303_SEE_OTHER)
