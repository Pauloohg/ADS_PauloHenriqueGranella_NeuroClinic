from datetime import datetime
from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.core.config import DADOS_CLINICA

DIRETORIO_APP = Path(__file__).resolve().parent.parent

# Não mover para app/main.py: os routers importam daqui e isso criaria import circular
templates = Jinja2Templates(directory=DIRETORIO_APP / "templates")


def url_estatico(caminho: str) -> str:
    arquivo = DIRETORIO_APP / "static" / caminho
    versao = int(arquivo.stat().st_mtime) if arquivo.exists() else 0
    return f"/static/{caminho}?v={versao}"


def ano_atual() -> int:
    return datetime.now().year


templates.env.globals["url_estatico"] = url_estatico
templates.env.globals["ano_atual"] = ano_atual
templates.env.globals["clinica"] = DADOS_CLINICA
