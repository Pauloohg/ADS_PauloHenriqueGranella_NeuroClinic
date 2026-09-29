import logging

from fastapi import Depends, FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import settings
from app.core.templating import templates
from app.models.usuario import Usuario
from app.routers import auth
from app.routers.auth import NaoAutenticado, erros_por_campo, get_usuario_opcional

if settings.DEBUG:
    # Sem isso o link de redefinição de senha (logado em auth_service) não aparece no console
    logging.basicConfig(level=logging.INFO)

app = FastAPI(title=settings.APP_NAME, debug=settings.DEBUG)

app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(auth.router)


@app.exception_handler(NaoAutenticado)
def redirecionar_para_login(request: Request, exc: NaoAutenticado):
    return RedirectResponse("/login", status_code=303)


# Toda rota POST de formulário HTML precisa estar aqui (chave = path do router, com {parâmetros}), senão o erro vira JSON 422
FORMULARIOS_POR_ROTA = {
    "/cadastro": "auth/cadastro.html",
    "/login": "auth/login.html",
    "/esqueci-senha": "auth/esqueci_senha.html",
    "/redefinir-senha/{token}": "auth/redefinir_senha.html",
}
CAMPOS_SENSIVEIS = {"senha", "nova_senha"}  # todo campo de senha novo precisa entrar aqui, senão volta preenchido no HTML
MSG_VALIDACAO_GERAL = "Alguns dados não foram aceitos. Confira os campos e tente novamente."


@app.exception_handler(RequestValidationError)
async def reexibir_formulario_com_erros(request: Request, exc: RequestValidationError):
    rota = request.scope.get("route")
    template = FORMULARIOS_POR_ROTA.get(getattr(rota, "path", None))
    if template is None or request.method != "POST":
        return await request_validation_exception_handler(request, exc)

    try:
        form = await request.form()
        dados = {k: v for k, v in form.items() if k not in CAMPOS_SENSIVEIS and isinstance(v, str)}
    except Exception:
        dados = {}

    contexto = {
        "dados": dados,
        "email": dados.get("email", ""),
        "token": request.path_params.get("token", ""),
        "erros": erros_por_campo(exc.errors()),
        "erro": MSG_VALIDACAO_GERAL,
    }
    return templates.TemplateResponse(request, template, contexto, status_code=400)


@app.get("/")
def home(request: Request, usuario: Usuario | None = Depends(get_usuario_opcional)):
    return templates.TemplateResponse(request, "index.html", {"usuario": usuario})
