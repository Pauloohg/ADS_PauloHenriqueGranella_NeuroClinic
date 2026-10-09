from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.core import security
from app.core.config import settings
from app.core.fuso import FUSO_CLINICA, utc_para_local  # noqa: F401 — reexportados: routers e testes importam daqui
from app.core.templating import templates
from app.db.session import get_db
from app.models.usuario import TipoUsuario, Usuario
from app.schemas.usuario import (
    ClienteCadastro,
    ConfirmacaoRedefinicaoSenha,
    Login,
    SolicitacaoRedefinicaoSenha,
)
from app.services import auth_service

router = APIRouter(tags=["auth"])

COOKIE_TOKEN = "access_token"

MSG_CREDENCIAIS_INVALIDAS = "E-mail ou senha inválidos."
MSG_LINK_EXPIRADO = "Este link de redefinição expirou. Solicite um novo."
MSG_LINK_INVALIDO = "Este link de redefinição é inválido ou já foi utilizado. Solicite um novo."


def agora_utc() -> datetime:
    return datetime.now(timezone.utc)


def agora_local() -> datetime:
    return utc_para_local(agora_utc())


MSG_CAMPO_OBRIGATORIO = "Este campo é obrigatório."
MSG_EMAIL_INVALIDO = "Informe um e-mail válido, como nome@exemplo.com."
MSG_DATA_INVALIDA = "Informe uma data válida."
MSG_ERRO_GENERICO = "Verifique este campo."

_ORIGENS_LOC = {"body", "query", "path", "header", "cookie"}


def mensagem_erro(campo: str, erro: dict) -> str:
    tipo = erro.get("type")
    entrada = erro.get("input")
    ctx = erro.get("ctx") or {}  # "ctx" e "ctx.error" nem sempre existem (ex.: EmailStr) — nunca indexar direto

    if tipo == "missing" or (isinstance(entrada, str) and not entrada.strip()):
        return MSG_CAMPO_OBRIGATORIO
    if campo == "email":
        return MSG_EMAIL_INVALIDO
    if isinstance(tipo, str) and tipo.startswith("date_"):
        return MSG_DATA_INVALIDA
    if tipo == "value_error" and ctx.get("error"):
        return str(ctx["error"])
    if tipo == "string_too_short" and "min_length" in ctx:
        return f"Deve ter pelo menos {ctx['min_length']} caracteres."
    if tipo == "string_too_long" and "max_length" in ctx:
        return f"Deve ter no máximo {ctx['max_length']} caracteres."
    return MSG_ERRO_GENERICO


def erros_por_campo(erros_pydantic: list[dict]) -> dict[str, str]:
    erros: dict[str, str] = {}
    for erro in erros_pydantic:
        loc = [parte for parte in erro.get("loc", ()) if parte not in _ORIGENS_LOC]
        campo = str(loc[0]) if loc else "geral"
        erros.setdefault(campo, mensagem_erro(campo, erro))
    return erros


# --- Dependencies de autenticação ----------------------------------------------------------------

class NaoAutenticado(Exception):
    pass


def _extrair_token(request: Request) -> str | None:
    token = request.cookies.get(COOKIE_TOKEN)
    if token:
        return token
    autorizacao = request.headers.get("Authorization", "")
    esquema, _, valor = autorizacao.partition(" ")
    if esquema.lower() == "bearer" and valor:
        return valor
    return None


def get_usuario_opcional(request: Request, db: Session = Depends(get_db)) -> Usuario | None:
    token = _extrair_token(request)
    if token is None:
        return None
    try:
        payload = security.decodificar_token_acesso(token, agora_utc())
    except security.TokenInvalido:
        return None
    return db.get(Usuario, int(payload["sub"]))


def get_usuario_atual(usuario: Usuario | None = Depends(get_usuario_opcional)) -> Usuario:
    if usuario is None:
        raise NaoAutenticado
    return usuario


def get_usuario_por_tipo(*tipos: TipoUsuario):
    def dependency(usuario: Usuario = Depends(get_usuario_atual)) -> Usuario:
        if usuario.tipo not in tipos:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Acesso não permitido para este perfil.")
        return usuario

    return dependency


# --- Cadastro ------------------------------------------------------------------------------------

@router.get("/cadastro")
def cadastro_form(request: Request):
    return templates.TemplateResponse(request, "auth/cadastro.html", {"dados": {}, "erros": {}})


@router.post("/cadastro")
def cadastro(
    request: Request,
    nome: Annotated[str, Form()] = "",
    email: Annotated[str, Form()] = "",
    telefone: Annotated[str, Form()] = "",
    senha: Annotated[str, Form()] = "",
    db: Session = Depends(get_db),
):
    dados_form = {"nome": nome, "email": email, "telefone": telefone}
    try:
        dados = ClienteCadastro(nome=nome, email=email, telefone=telefone, senha=senha)
        auth_service.cadastrar_cliente(db, dados, agora_utc())
    except ValidationError as exc:
        return templates.TemplateResponse(
            request, "auth/cadastro.html", {"dados": dados_form, "erros": erros_por_campo(exc.errors())},
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    except auth_service.EmailJaCadastrado:
        return templates.TemplateResponse(
            request, "auth/cadastro.html",
            {"dados": dados_form, "erros": {"email": "Já existe uma conta com este e-mail."}},
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    return RedirectResponse("/login?cadastro=ok", status_code=status.HTTP_303_SEE_OTHER)


# --- Login / Logout ------------------------------------------------------------------------------

@router.get("/login")
def login_form(request: Request):
    return templates.TemplateResponse(request, "auth/login.html", {"email": ""})


@router.post("/login")
def login(
    request: Request,
    email: Annotated[str, Form()] = "",
    senha: Annotated[str, Form()] = "",
    db: Session = Depends(get_db),
):
    try:
        token = auth_service.autenticar(db, Login(email=email, senha=senha), agora_utc())
    except (ValidationError, auth_service.CredenciaisInvalidas):
        return templates.TemplateResponse(
            request, "auth/login.html", {"email": email, "erro": MSG_CREDENCIAIS_INVALIDAS},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    resposta = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    resposta.set_cookie(
        COOKIE_TOKEN,
        token,
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        httponly=True,
        samesite="lax",
        secure=not settings.DEBUG,
    )
    return resposta


@router.post("/logout")
def logout():
    resposta = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    resposta.delete_cookie(COOKIE_TOKEN)
    return resposta


# --- Redefinição de senha ------------------------------------------------------------------------

@router.get("/esqueci-senha")
def esqueci_senha_form(request: Request):
    return templates.TemplateResponse(request, "auth/esqueci_senha.html", {"email": ""})


@router.post("/esqueci-senha")
def esqueci_senha(
    request: Request,
    tarefas: BackgroundTasks,
    email: Annotated[str, Form()] = "",
    db: Session = Depends(get_db),
):
    try:
        dados = SolicitacaoRedefinicaoSenha(email=email)
    except ValidationError:
        return templates.TemplateResponse(
            request, "auth/esqueci_senha.html", {"email": email, "erro": "Informe um e-mail válido."},
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    auth_service.solicitar_redefinicao_senha(db, dados, str(request.base_url), agora_utc(), tarefas)
    # Não diferenciar e-mail existente de inexistente na resposta (HU02: enumeração de contas)
    return templates.TemplateResponse(request, "auth/esqueci_senha.html", {"email": "", "enviado": True})


@router.get("/redefinir-senha/{token}")
def redefinir_senha_form(request: Request, token: str, db: Session = Depends(get_db)):
    try:
        auth_service.validar_token_redefinicao(db, token, agora_utc())
    except security.TokenExpirado:
        return _link_invalido(request, MSG_LINK_EXPIRADO)
    except security.TokenInvalido:
        return _link_invalido(request, MSG_LINK_INVALIDO)
    return templates.TemplateResponse(request, "auth/redefinir_senha.html", {"token": token, "erros": {}})


@router.post("/redefinir-senha/{token}")
def redefinir_senha(
    request: Request, token: str, nova_senha: Annotated[str, Form()] = "", db: Session = Depends(get_db)
):
    try:
        dados = ConfirmacaoRedefinicaoSenha(token=token, nova_senha=nova_senha)
        auth_service.redefinir_senha(db, dados, agora_utc())
    except ValidationError as exc:
        return templates.TemplateResponse(
            request, "auth/redefinir_senha.html", {"token": token, "erros": erros_por_campo(exc.errors())},
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    except security.TokenExpirado:
        return _link_invalido(request, MSG_LINK_EXPIRADO)
    except security.TokenInvalido:
        return _link_invalido(request, MSG_LINK_INVALIDO)
    return RedirectResponse("/login?senha=redefinida", status_code=status.HTTP_303_SEE_OTHER)


def _link_invalido(request: Request, mensagem: str):
    return templates.TemplateResponse(
        request, "auth/esqueci_senha.html", {"email": "", "erro": mensagem},
        status_code=status.HTTP_400_BAD_REQUEST,
    )
