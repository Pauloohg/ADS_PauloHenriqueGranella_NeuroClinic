import re
from datetime import date, datetime
from pathlib import Path

import pytest

from app.core.ids import INT4_MAX, inteiro_positivo, ler_inteiro, somente_digitos
from app.models.sessao import StatusSessao
from app.routers import agenda as agenda_router
from app.routers import consultas as consultas_router
from app.routers import historico as historico_router
from app.routers import meus_atendidos as meus_atendidos_router
from tests.fabricas import (
    criar_admin,
    criar_cliente,
    criar_especialidade,
    criar_funcionario,
    criar_paciente,
    criar_plano,
    logar,
)

AGORA = datetime(2026, 10, 28, 15, 0)
INVALIDOS = ["²", "٣", "2147483648", "1" * 30]
COMUM = "abc"


# --- Funções de app/core/ids.py ---------------------------------------------------------------------

@pytest.mark.parametrize("texto, esperado", [
    ("1", 1), ("42", 42), ("007", 7), (str(INT4_MAX), INT4_MAX), ("0" * 50 + "5", 5),
])
def test_inteiro_positivo_aceita_digitos_ascii(texto, esperado):
    assert inteiro_positivo(texto) == esperado


@pytest.mark.parametrize("texto", [
    None, "", " ", "0", "00", "-1", "+1", "1.0", "1e3", " 1", "1 ", "abc", "²", "¹²", "٣", "١٢٣", "１２",
    str(INT4_MAX + 1), "9" * 30, "9" * 5000, "1_000", "0x10",
])
def test_inteiro_positivo_recusa_o_resto_sem_lancar_excecao(texto):
    assert inteiro_positivo(texto) is None


def test_inteiro_positivo_respeita_o_maximo_informado():
    assert inteiro_positivo("10", maximo=10) == 10
    assert inteiro_positivo("11", maximo=10) is None


def test_ler_inteiro_aceita_zero_quando_o_minimo_permite():
    assert ler_inteiro("0") == 0 and ler_inteiro("23", maximo=23) == 23
    assert ler_inteiro("0", minimo=1) is None and ler_inteiro("٠") is None


def test_somente_digitos_ignora_digitos_nao_ascii():
    assert somente_digitos("(54) 99999-1234") == "54999991234"
    assert somente_digitos("1²٣4") == "14" and somente_digitos(None) == ""


def test_nenhum_isdigit_no_app_fora_de_ids():
    padrao = re.compile(r"\.(isdigit|isdecimal|isnumeric)\(")
    permitido = Path("app/core/ids.py")
    achados = [
        f"{arquivo}:{numero}"
        for arquivo in sorted(Path("app").rglob("*.py")) if arquivo != permitido
        for numero, linha in enumerate(arquivo.read_text(encoding="utf-8").splitlines(), 1)
        if padrao.search(linha)
    ]
    assert achados == [], "use app/core/ids.py em vez de isdigit/isdecimal/isnumeric"


# --- Regressão por rota: valores estranhos respondem como "abc" e nunca 500 ------------------------

@pytest.fixture(autouse=True)
def relogio_fixo(monkeypatch):
    for modulo in (agenda_router, consultas_router, historico_router, meus_atendidos_router):
        monkeypatch.setattr(modulo, "agora_local", lambda: AGORA)


@pytest.fixture
def cenario(db):
    fono = criar_funcionario(db, criar_especialidade(db, "Fonoaudiologia"))
    cliente = criar_cliente(db)
    paciente = criar_paciente(db, cliente.id, nascimento=date(2018, 10, 29))
    plano = criar_plano(db, paciente, fono, [(datetime(2026, 10, 21, 9, 0), StatusSessao.REALIZADA),
                                             (datetime(2026, 11, 4, 9, 0), StatusSessao.AGENDADA)])
    return {
        "cliente": cliente.email, "funcionario": fono.usuario.email, "admin": criar_admin(db).email,
        "fid": fono.usuario.id, "pid": paciente.id, "plano": plano.id,
    }


def _simulacao(fid, dias="1", frequencia="1"):
    return f"/contratar/{fid}/simulacao?dias={dias}&horario=09:00&frequencia={frequencia}&duracao=mensal"


# perfil, método, rota (com {v} no lugar do valor), formulário
ROTAS = {
    "admin_clientes_pagina": ("admin", "GET", "/admin/clientes?pagina={v}", None),
    "admin_painel_mes": ("admin", "GET", "/admin/painel?mes={v}", None),
    "agenda_registrar_pagina": ("funcionario", "GET", "/agenda?visao=registrar&pagina={v}", None),
    "meus_atendidos_pagina": ("funcionario", "GET", "/meus-atendidos?pagina={v}", None),
    "meus_atendidos_id": ("funcionario", "GET", "/meus-atendidos/{v}", None),
    "meus_atendidos_detalhe_pagina": ("funcionario", "GET", "/meus-atendidos/{pid}?pagina={v}", None),
    "consultas_pagina": ("cliente", "GET", "/consultas?pagina={v}", None),
    "consultas_remarcada": ("cliente", "GET", "/consultas?remarcada={v}", None),
    "historico_pagina": ("cliente", "GET", "/historico?pagina={v}", None),
    "contratar_paciente": ("cliente", "POST", "/contratar/{fid}", {"paciente_id": "{v}"}),
    "contratar_confirmar_paciente": ("cliente", "POST", "/contratar/{fid}/confirmar", {"paciente_id": "{v}"}),
    "simulacao_dias": ("cliente", "GET", "SIMULACAO_DIAS", None),
    "simulacao_frequencia": ("cliente", "GET", "SIMULACAO_FREQUENCIA", None),
    "renovar_profissional": ("cliente", "GET", "/planos/{plano}/renovar?profissional={v}", None),
}


def _pedir(client, cenario, rota, valor):
    perfil, metodo, url, formulario = ROTAS[rota]
    if url == "SIMULACAO_DIAS":
        url = _simulacao(cenario["fid"], dias=valor)
    elif url == "SIMULACAO_FREQUENCIA":
        url = _simulacao(cenario["fid"], frequencia=valor)
    else:
        url = url.format(v=valor, fid=cenario["fid"], pid=cenario["pid"], plano=cenario["plano"])
    dados = {k: x.replace("{v}", valor) for k, x in formulario.items()} if formulario else None
    return client.request(metodo, url, data=dados, follow_redirects=False)


def _resumo(resposta):
    if resposta.headers.get("content-type", "").startswith("application/json"):
        return resposta.status_code, resposta.json()
    return resposta.status_code, resposta.headers.get("location")


@pytest.mark.parametrize("valor", INVALIDOS, ids=["expoente", "arabico", "int4_mais_1", "30_digitos"])
@pytest.mark.parametrize("rota", ROTAS)
def test_parametro_invalido_responde_como_abc_e_nunca_500(db, client, cenario, rota, valor):
    logar(client, cenario[ROTAS[rota][0]])
    referencia = _pedir(client, cenario, rota, COMUM)
    resposta = _pedir(client, cenario, rota, valor)
    assert resposta.status_code != 500
    assert _resumo(resposta) == _resumo(referencia)


@pytest.mark.parametrize("rota", ["consultas_pagina", "historico_pagina", "meus_atendidos_pagina", "admin_clientes_pagina"])
def test_pagina_acima_do_limite_cai_na_primeira(db, client, cenario, rota):
    logar(client, cenario[ROTAS[rota][0]])
    primeira = _pedir(client, cenario, rota, "1")
    for valor in ("2147483648", "9" * 30):
        resposta = _pedir(client, cenario, rota, valor)
        assert resposta.status_code == 200 and resposta.text == primeira.text
