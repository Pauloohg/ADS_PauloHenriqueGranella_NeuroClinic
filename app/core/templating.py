import unicodedata
from datetime import datetime, timedelta
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


NOMES_DIAS = ("Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo")
ROTULOS_DURACAO = {
    "quinzenal": "Quinzenal (2 semanas)",
    "mensal": "Mensal (1 mês)",
    "bimestral": "Bimestral (2 meses)",
    "anual": "Anual (12 meses)",
}


def nome_dia(dia: int) -> str:
    return NOMES_DIAS[dia]


def data_br(valor) -> str:
    return valor.strftime("%d/%m/%Y")


def primeiro_nome(nome: str) -> str:
    return nome.split()[0] if nome.split() else nome


def tempo_curto(intervalo: timedelta) -> str:
    minutos = int(intervalo.total_seconds() // 60)
    horas, minutos = divmod(minutos, 60)
    if horas and minutos:
        return f"{horas}h {minutos:02d}min"
    if horas:
        return f"{horas}h"
    return f"{minutos}min" if minutos else "menos de 1min"


def iniciais(nome: str) -> str:
    partes = nome.split()
    return "".join(parte[0] for parte in (partes[:1] + partes[1:][-1:])).upper()


templates.env.globals["url_estatico"] = url_estatico
templates.env.globals["ano_atual"] = ano_atual
templates.env.globals["clinica"] = DADOS_CLINICA
templates.env.filters["moeda"] = moeda
templates.env.filters["valor_formulario"] = valor_formulario
templates.env.filters["classe_especialidade"] = classe_especialidade
templates.env.filters["iniciais"] = iniciais
templates.env.filters["primeiro_nome"] = primeiro_nome
templates.env.filters["tempo_curto"] = tempo_curto
templates.env.filters["nome_dia"] = nome_dia
templates.env.filters["data_br"] = data_br
templates.env.globals["rotulos_duracao"] = ROTULOS_DURACAO
