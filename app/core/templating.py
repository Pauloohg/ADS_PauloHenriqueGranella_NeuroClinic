import unicodedata
from datetime import datetime
from decimal import Decimal
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


def moeda(valor: Decimal) -> str:
    return "R$ " + f"{valor:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


def valor_formulario(valor: Decimal) -> str:
    return f"{valor:.2f}".replace(".", ",")


# Só as especialidades com paleta definida em styles.css (.esp-*) ganham cor; as demais usam o chip neutro
CLASSES_ESPECIALIDADE = {"psicopedagogia", "fonoaudiologia", "psicologia"}


def classe_especialidade(nome: str) -> str:
    chave = unicodedata.normalize("NFKD", nome).encode("ascii", "ignore").decode().lower().strip()
    return f"esp-{chave}" if chave in CLASSES_ESPECIALIDADE else ""


templates.env.globals["url_estatico"] = url_estatico
templates.env.globals["ano_atual"] = ano_atual
templates.env.globals["clinica"] = DADOS_CLINICA
templates.env.filters["moeda"] = moeda
templates.env.filters["valor_formulario"] = valor_formulario
templates.env.filters["classe_especialidade"] = classe_especialidade
