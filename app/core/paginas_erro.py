import html
import logging
from dataclasses import dataclass

from fastapi import Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import DADOS_CLINICA
from app.core.templating import templates

logger = logging.getLogger(__name__)

TEMPLATE_ERRO = "erros/erro.html"


@dataclass(frozen=True)
class TextoDeErro:
    titulo: str
    texto: str
    icone: str


TEXTO_404 = "Não encontramos o que você procurou. O endereço pode estar errado ou o conteúdo não está disponível para a sua conta."
TEXTOS_POR_STATUS = {
    400: TextoDeErro("Dados inválidos", "Os dados enviados contêm caracteres inválidos.", "bi-exclamation-triangle"),
    403: TextoDeErro("Acesso não permitido", "Esta página não está disponível para o seu tipo de conta.", "bi-shield-lock"),
    404: TextoDeErro("Página não encontrada", TEXTO_404, "bi-signpost-split"),
    405: TextoDeErro("Ação não permitida", "Não foi possível fazer isso por aqui.", "bi-slash-circle"),
    422: TextoDeErro("Endereço inválido", "O endereço ou os dados enviados não são válidos.", "bi-link-45deg"),
    500: TextoDeErro(
        "Algo deu errado do nosso lado",
        "Tente de novo em instantes. Se você estava confirmando um plano, um pagamento ou uma remarcação, "
        "confira em Início ou Consultas antes de tentar outra vez.",
        "bi-tools",
    ),
}
TEXTO_GENERICO = TextoDeErro("Não foi possível abrir esta página", "Tente de novo ou volte ao início.", "bi-info-circle")
STATUS_COM_ENTRAR = {403, 404}


def texto_do_erro(status_code: int) -> TextoDeErro:
    if status_code in TEXTOS_POR_STATUS:
        return TEXTOS_POR_STATUS[status_code]
    return TEXTOS_POR_STATUS[500] if status_code >= 500 else TEXTO_GENERICO


def pede_html(accept: str | None) -> bool:
    for parte in (accept or "").split(","):
        tipo, *parametros = (pedaco.strip() for pedaco in parte.split(";"))
        if tipo.lower() != "text/html":
            continue
        q = next((p[2:].strip() for p in parametros if p.lower().startswith("q=")), "1")
        try:
            return float(q) > 0
        except ValueError:
            return True
    return False


def linha_de_contato(clinica: dict = DADOS_CLINICA) -> str:
    telefone = clinica["telefone"].replace(" ", "\u00a0")  # o número nunca quebra no meio da linha
    return f"Se o problema continuar, fale com a {clinica['nome']} pelo telefone {telefone} ({clinica['horario']})."


def html_minimo(status_code: int) -> str:
    titulo = html.escape(texto_do_erro(status_code).titulo)
    return (
        '<!DOCTYPE html><html lang="pt-br"><head><meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
        f"<title>{titulo} — NeuroClinic</title></head>"
        f'<body style="font-family: sans-serif; padding: 2rem;"><h1>{titulo}</h1>'
        '<p><a href="/">Ir para a página inicial</a></p></body></html>'
    )


def _usuario_ou_visitante(request: Request):
    from app.db.session import get_db
    from app.routers.auth import get_usuario_opcional

    try:
        fonte = request.app.dependency_overrides.get(get_db, get_db)()
        if not hasattr(fonte, "__next__"):
            return get_usuario_opcional(request, fonte)
        try:
            return get_usuario_opcional(request, next(fonte))
        finally:
            fonte.close()
    except Exception:
        return None


def _montar_pagina(request: Request, status_code: int, headers: dict | None) -> Response:
    cabecalhos = dict(headers or {})
    if status_code >= 500:
        cabecalhos["Cache-Control"] = "no-store"
    try:
        usuario = _usuario_ou_visitante(request)
        contexto = {
            "usuario": usuario,
            "erro": texto_do_erro(status_code),
            "contato": linha_de_contato() if status_code >= 500 else None,
            "mostrar_entrar": usuario is None and status_code in STATUS_COM_ENTRAR,
        }
        return templates.TemplateResponse(request, TEMPLATE_ERRO, contexto, status_code=status_code, headers=cabecalhos)
    except Exception:
        logger.exception("Falha ao renderizar a página de erro %s", status_code)
        return HTMLResponse(html_minimo(status_code), status_code=status_code, headers=cabecalhos)


async def pagina_de_erro(request: Request, status_code: int, headers: dict | None = None) -> Response:
    try:
        return await run_in_threadpool(_montar_pagina, request, status_code, headers)
    except Exception:
        return HTMLResponse(html_minimo(status_code), status_code=status_code, headers=dict(headers or {}))


async def tratar_http_exception(request: Request, exc: StarletteHTTPException) -> Response:
    if exc.status_code < 400 or not pede_html(request.headers.get("accept")):
        return await http_exception_handler(request, exc)
    return await pagina_de_erro(request, exc.status_code, getattr(exc, "headers", None))


async def tratar_validacao_sem_formulario(request: Request, exc: RequestValidationError) -> Response:
    if not pede_html(request.headers.get("accept")):
        return await request_validation_exception_handler(request, exc)
    return await pagina_de_erro(request, 422)


def _caminho_para_log(scope: Scope) -> str:
    rota = scope.get("route")
    return getattr(rota, "path", None) or scope.get("path", "?")  # o molde da rota evita gravar token ou id no log


class ErroInesperado:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        iniciada = False

        async def enviar(mensagem: Message) -> None:
            nonlocal iniciada
            if mensagem["type"] == "http.response.start":
                iniciada = True
            await send(mensagem)

        try:
            await self.app(scope, receive, enviar)
        except Exception:
            request = Request(scope)
            # Sem text/html (ou resposta já começada) o erro segue para o ServerErrorMiddleware, como antes
            if iniciada or not pede_html(request.headers.get("accept")):
                raise
            logger.exception("Erro não previsto em %s %s", scope.get("method"), _caminho_para_log(scope))
            resposta = await pagina_de_erro(request, 500)
            await resposta(scope, receive, send)
