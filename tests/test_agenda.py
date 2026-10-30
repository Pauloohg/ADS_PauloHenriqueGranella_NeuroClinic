import re
from datetime import date, datetime

import pytest
from sqlalchemy import event

from app.models.cliente import Cliente
from app.models.sessao import Sessao, StatusSessao
from app.routers import agenda as agenda_router
from app.routers import inicio as inicio_router
from app.services import agenda_service, funcionario_service
from app.services.agenda_service import DIA, PERIODO, SEMANA, PeriodoInvalido, Visao
from tests.fabricas import (
    criar_admin,
    criar_cliente,
    criar_especialidade,
    criar_funcionario,
    criar_paciente,
    criar_plano,
    logar,
)

AGENDADA = StatusSessao.AGENDADA
PENDENTE = StatusSessao.PENDENTE_PAGAMENTO
REALIZADA = StatusSessao.REALIZADA

AGORA = datetime(2026, 10, 28, 9, 0)  # horário local, quarta-feira
HOJE = AGORA.date()
SEMANA_ATUAL = Visao(SEMANA, date(2026, 10, 26), date(2026, 10, 30))


@pytest.fixture(autouse=True)
def relogio_fixo(monkeypatch):
    monkeypatch.setattr(agenda_router, "agora_local", lambda: AGORA)
    monkeypatch.setattr(inicio_router, "agora_local", lambda: AGORA)


@pytest.fixture
def fono(db):
    return criar_funcionario(db, criar_especialidade(db, "Fonoaudiologia"))


@pytest.fixture
def psico(db):
    return criar_funcionario(db, criar_especialidade(db, "Psicologia"), email="psi@exemplo.com", nome="Paula Psi")


@pytest.fixture
def cliente(db):
    return criar_cliente(db, nome="Maria Responsável")


@pytest.fixture
def paciente(db, cliente):
    return criar_paciente(db, cliente.id, nome="Pedro Souza", nascimento=date(2018, 10, 29))


def _html(resposta) -> str:
    return " ".join(resposta.text.split())


def _agenda(client, **params) -> str:
    resposta = client.get("/agenda", params=params)
    assert resposta.status_code == 200
    return _html(resposta)


# --- Intervalo de cada visão (função pura) -------------------------------------------------------

def test_visao_dia_e_a_propria_data():
    assert agenda_service.intervalo_da_visao(DIA, date(2026, 10, 28)) == Visao(DIA, date(2026, 10, 28), date(2026, 10, 28))


@pytest.mark.parametrize("referencia, inicio, fim", [
    (date(2026, 10, 26), date(2026, 10, 26), date(2026, 10, 30)),  # segunda
    (date(2026, 10, 28), date(2026, 10, 26), date(2026, 10, 30)),  # quarta
    (date(2026, 10, 30), date(2026, 10, 26), date(2026, 10, 30)),  # sexta
    (date(2026, 11, 1), date(2026, 10, 26), date(2026, 10, 30)),  # domingo: semana que começou na segunda anterior
    (date(2026, 7, 1), date(2026, 6, 29), date(2026, 7, 3)),  # virada de mês
    (date(2026, 12, 31), date(2026, 12, 28), date(2027, 1, 1)),  # virada de ano
])
def test_visao_semana_vai_de_segunda_a_sexta(referencia, inicio, fim):
    assert agenda_service.intervalo_da_visao(SEMANA, referencia) == Visao(SEMANA, inicio, fim)


@pytest.mark.parametrize("inicio, fim", [
    (date(2026, 10, 28), date(2026, 10, 28)),  # um dia só
    (date(2026, 12, 20), date(2027, 1, 10)),  # virada de ano
    (date(2026, 1, 1), date(2026, 4, 2)),  # exatamente 92 dias
])
def test_visao_periodo_aceita_ate_92_dias(inicio, fim):
    assert agenda_service.intervalo_da_visao(PERIODO, HOJE, inicio, fim) == Visao(PERIODO, inicio, fim)


@pytest.mark.parametrize("inicio, fim, mensagem", [
    (date(2026, 1, 1), date(2026, 4, 3), agenda_service.MSG_PERIODO_LONGO),  # 93 dias
    (date(2026, 10, 28), date(2026, 10, 27), agenda_service.MSG_PERIODO_INVERTIDO),
    (None, date(2026, 10, 27), agenda_service.MSG_PERIODO_INCOMPLETO),
    (date(2026, 10, 28), None, agenda_service.MSG_PERIODO_INCOMPLETO),
])
def test_visao_periodo_invalida(inicio, fim, mensagem):
    with pytest.raises(PeriodoInvalido) as erro:
        agenda_service.intervalo_da_visao(PERIODO, HOJE, inicio, fim)
    assert erro.value.mensagem == mensagem


@pytest.mark.parametrize("visao, data, inicio, fim, esperada", [
    (None, None, None, None, SEMANA_ATUAL),
    ("qualquer", None, None, None, SEMANA_ATUAL),
    ("dia", "2026-11-03", None, None, Visao(DIA, date(2026, 11, 3), date(2026, 11, 3))),
    ("dia", None, None, None, Visao(DIA, HOJE, HOJE)),
    ("semana", "2026-12-31", None, None, Visao(SEMANA, date(2026, 12, 28), date(2027, 1, 1))),
    ("periodo", None, "2026-10-01", "2026-12-31", Visao(PERIODO, date(2026, 10, 1), date(2026, 12, 31))),
])
def test_montar_visao_valida(visao, data, inicio, fim, esperada):
    assert agenda_service.montar_visao(visao, data, inicio, fim, HOJE) == (esperada, None)


@pytest.mark.parametrize("visao, data, inicio, fim, mensagem", [
    ("dia", "2026-02-30", None, None, agenda_service.MSG_DATA_INVALIDA),
    ("semana", "abc", None, None, agenda_service.MSG_DATA_INVALIDA),
    ("dia", "9999-12-31", None, None, agenda_service.MSG_DATA_INVALIDA),
    ("periodo", None, "2026-10-01", "2027-01-01", agenda_service.MSG_PERIODO_LONGO),
    ("periodo", None, "2026-10-10", "2026-10-01", agenda_service.MSG_PERIODO_INVERTIDO),
    ("periodo", None, "2026-13-01", "2026-10-01", agenda_service.MSG_DATA_INVALIDA),
    ("periodo", None, "", "2026-10-01", agenda_service.MSG_PERIODO_INCOMPLETO),
])
def test_montar_visao_invalida_volta_para_a_semana_atual(visao, data, inicio, fim, mensagem):
    assert agenda_service.montar_visao(visao, data, inicio, fim, HOJE) == (SEMANA_ATUAL, mensagem)


@pytest.mark.parametrize("atual, sentido, esperada", [
    (Visao(DIA, date(2026, 10, 30), date(2026, 10, 30)), 1, Visao(DIA, date(2026, 11, 2), date(2026, 11, 2))),
    (Visao(DIA, date(2026, 10, 26), date(2026, 10, 26)), -1, Visao(DIA, date(2026, 10, 23), date(2026, 10, 23))),
    (Visao(SEMANA, date(2026, 12, 28), date(2027, 1, 1)), 1, Visao(SEMANA, date(2027, 1, 4), date(2027, 1, 8))),
    (Visao(PERIODO, date(2026, 10, 1), date(2026, 10, 10)), 1, Visao(PERIODO, date(2026, 10, 11), date(2026, 10, 20))),
    (Visao(PERIODO, date(2026, 10, 1), date(2026, 10, 10)), -1, Visao(PERIODO, date(2026, 9, 21), date(2026, 9, 30))),
])
def test_deslocar_visao_mantem_o_tipo(atual, sentido, esperada):
    assert agenda_service.deslocar_visao(atual, sentido) == esperada


def test_navegacao_hoje_mantem_a_visao_e_o_tamanho_do_periodo():
    periodo = Visao(PERIODO, date(2026, 1, 1), date(2026, 1, 10))
    assert agenda_service.navegacao(periodo, HOJE)["hoje"] == {"visao": "periodo", "inicio": "2026-10-28", "fim": "2026-11-06"}
    dia = Visao(DIA, date(2026, 1, 5), date(2026, 1, 5))
    assert agenda_service.navegacao(dia, HOJE)["hoje"] == {"visao": "dia", "data": "2026-10-28"}
    semana = agenda_service.navegacao(SEMANA_ATUAL, HOJE)
    assert semana["anterior"] == {"visao": "semana", "data": "2026-10-19"}
    assert semana["proximo"] == {"visao": "semana", "data": "2026-11-02"}


# --- Busca das consultas ------------------------------------------------------------------------

def test_idade_calculada_pela_data_de_referencia(db, fono, paciente):
    criar_plano(db, paciente, fono, [(datetime(2026, 10, 28, 14, 0), AGENDADA)])
    na_vespera = agenda_service.consultas_do_profissional(db, fono.usuario.id, HOJE, HOJE, date(2026, 10, 28))
    no_aniversario = agenda_service.consultas_do_profissional(db, fono.usuario.id, HOJE, HOJE, date(2026, 10, 29))
    assert na_vespera[0].idade == 7
    assert no_aniversario[0].idade == 8


def test_consulta_tem_inicio_e_fim_de_60_minutos(db, fono, paciente):
    criar_plano(db, paciente, fono, [(datetime(2026, 10, 28, 14, 0), AGENDADA)])
    consulta, = agenda_service.consultas_do_profissional(db, fono.usuario.id, HOJE, HOJE, HOJE)
    assert (consulta.inicio, consulta.fim) == (datetime(2026, 10, 28, 14, 0), datetime(2026, 10, 28, 15, 0))


def test_intervalo_inclui_o_ultimo_dia_inteiro(db, fono, paciente):
    criar_plano(db, paciente, fono, [
        (datetime(2026, 10, 25, 17, 0), AGENDADA),  # domingo antes da semana: fora
        (datetime(2026, 10, 26, 8, 0), AGENDADA),
        (datetime(2026, 10, 30, 17, 0), AGENDADA),
        (datetime(2026, 11, 2, 8, 0), AGENDADA),  # segunda seguinte: fora
    ])
    consultas = agenda_service.consultas_do_profissional(db, fono.usuario.id, date(2026, 10, 26), date(2026, 10, 30), HOJE)
    assert [c.inicio for c in consultas] == [datetime(2026, 10, 26, 8, 0), datetime(2026, 10, 30, 17, 0)]


def test_busca_faz_uma_unica_consulta_ao_banco(db, fono, cliente):
    for i in range(5):
        atendido = criar_paciente(db, cliente.id, nome=f"Atendido {i}")
        criar_plano(db, atendido, fono, [(datetime(2026, 10, 26 + i, 14, 0), AGENDADA)])
    funcionario_id = fono.usuario.id
    db.expire_all()
    comandos = []
    ouvinte = lambda *args: comandos.append(args[2])  # noqa: E731
    event.listen(db.get_bind(), "before_cursor_execute", ouvinte)
    try:
        consultas = agenda_service.consultas_do_profissional(db, funcionario_id, date(2026, 10, 26), date(2026, 10, 30), HOJE)
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", ouvinte)
    assert len(consultas) == 5
    assert len(comandos) == 1


def test_contar_consultas_do_dia_ignora_pendente_e_outro_profissional(db, fono, psico, paciente):
    criar_plano(db, paciente, fono, [(datetime(2026, 10, 28, 8, 0), REALIZADA), (datetime(2026, 10, 28, 14, 0), AGENDADA)])
    criar_plano(db, paciente, fono, [(datetime(2026, 10, 28, 15, 0), PENDENTE)])
    criar_plano(db, paciente, psico, [(datetime(2026, 10, 28, 16, 0), AGENDADA)])
    assert agenda_service.contar_consultas_do_dia(db, fono.usuario.id, HOJE) == 2


# --- Tela da agenda ------------------------------------------------------------------------------

def test_profissional_a_nunca_ve_consulta_do_profissional_b(client, db, fono, psico, cliente):
    do_a = criar_paciente(db, cliente.id, nome="Ana do Fono")
    do_b = criar_paciente(db, cliente.id, nome="Bruno da Psico")
    criar_plano(db, do_a, fono, [(datetime(2026, 10, 27, 14, 0), AGENDADA)])
    criar_plano(db, do_b, psico, [(datetime(2026, 10, 27, 15, 0), AGENDADA)])

    logar(client, fono.usuario.email)
    for params in ({}, {"visao": "dia", "data": "2026-10-27"}, {"visao": "periodo", "inicio": "2026-10-01", "fim": "2026-12-31"}):
        html = _agenda(client, **params)
        assert "Ana do Fono" in html
        assert "Bruno da Psico" not in html


def test_pendente_pagamento_nao_aparece_e_agendada_e_realizada_sim(client, db, fono, cliente):
    for nome, status, dia in [("Atendido Pago", AGENDADA, 29), ("Atendido Atendido", REALIZADA, 26), ("Atendido Pendente", PENDENTE, 30)]:
        criar_plano(db, criar_paciente(db, cliente.id, nome=nome), fono, [(datetime(2026, 10, dia, 9, 0), status)])

    logar(client, fono.usuario.email)
    html = _agenda(client)
    assert "Atendido Pago" in html and "Atendido Atendido" in html
    assert "Atendido Pendente" not in html
    assert html.count('status-ativo">Agendada<') == 1
    assert html.count('status-inativo">Realizada<') == 1
    assert "2 consultas" in html


def test_consulta_remarcada_aparece_so_na_nova_data(client, db, fono, paciente):
    plano = criar_plano(db, paciente, fono, [(datetime(2026, 10, 27, 14, 0), AGENDADA)])
    sessao = db.query(Sessao).filter_by(plano_id=plano.id).one()
    sessao.data_hora, sessao.foi_remarcada = datetime(2026, 10, 29, 10, 0), True  # como a remarcação (RF07) fará
    db.commit()

    logar(client, fono.usuario.email)
    original = _agenda(client, visao="dia", data="2026-10-27")
    nova = _agenda(client, visao="dia", data="2026-10-29")
    assert "Pedro Souza" not in original and "Nenhuma consulta neste dia" in original
    assert "Pedro Souza" in nova and 'status-remarcada">Remarcada<' in nova
    assert "10:00" in nova and "11:00" in nova


def test_consulta_mostra_horario_atendido_idade_especialidade_e_observacao(client, db, fono, paciente):
    plano = criar_plano(db, paciente, fono, [(datetime(2026, 10, 28, 14, 0), AGENDADA)])
    plano.observacao = "Prefere atividades com música."
    db.commit()

    logar(client, fono.usuario.email)
    html = _agenda(client, visao="dia", data="2026-10-28")
    assert re.search(r"14:00.*?15:00", html)
    assert "Pedro Souza" in html and "7 anos" in html
    assert 'chip-especialidade">Fonoaudiologia<' in html
    assert "Observação do plano:" in html and "Prefere atividades com música." in html


def test_sem_observacao_nao_mostra_o_rotulo(client, db, fono, paciente):
    criar_plano(db, paciente, fono, [(datetime(2026, 10, 28, 14, 0), AGENDADA)])
    logar(client, fono.usuario.email)
    assert "Observação do plano" not in _agenda(client)


def test_observacao_sai_escapada(client, db, fono, paciente):
    plano = criar_plano(db, paciente, fono, [(datetime(2026, 10, 28, 14, 0), AGENDADA)])
    plano.observacao = "<script>alert('x')</script>"
    db.commit()

    logar(client, fono.usuario.email)
    html = client.get("/agenda").text
    assert "<script>alert" not in html
    assert "&lt;script&gt;alert(" in html


def test_nao_exibe_dados_do_cliente_nem_nascimento_completo(client, db, fono, cliente, paciente):
    db.get(Cliente, cliente.id).cpf = "123.456.789-09"
    plano = criar_plano(db, paciente, fono, [(datetime(2026, 10, 28, 14, 0), AGENDADA)])
    plano.observacao = "Observação qualquer"
    db.commit()

    logar(client, fono.usuario.email)
    html = _agenda(client)
    assert "Pedro Souza" in html
    for proibido in ["123.456.789-09", "12345678909", "(54) 99999-1234", "99999-1234", cliente.email,
                     "Maria Responsável", "29/10/2018", "2018-10-29"]:
        assert proibido not in html, proibido


def test_semana_agrupa_por_dia_e_destaca_hoje(client, db, fono, paciente):
    criar_plano(db, paciente, fono, [(datetime(2026, 10, 26, 9, 0), AGENDADA), (datetime(2026, 10, 29, 9, 0), AGENDADA)])
    logar(client, fono.usuario.email)
    html = _agenda(client)

    titulos = re.findall(r'agenda-dia-nome">(\w+)</span> <span class="font-monospace-num">([\d/]+)<', html)
    assert titulos == [("Segunda", "26/10/2026"), ("Terça", "27/10/2026"), ("Quarta", "28/10/2026"),
                       ("Quinta", "29/10/2026"), ("Sexta", "30/10/2026")]
    assert html.count("Sem consultas") == 3
    assert re.search(r'agenda-dia agenda-dia-hoje[^"]*" aria-labelledby="dia-2026-10-28"', html)
    assert html.count("agenda-dia-hoje") == 1


def test_feriado_nacional_aparece_no_dia(client, db, fono, paciente):
    criar_plano(db, paciente, fono, [(datetime(2026, 11, 3, 9, 0), AGENDADA)])
    logar(client, fono.usuario.email)
    html = _agenda(client, visao="semana", data="2026-11-02")  # 02/11 Finados
    assert re.search(r'id="dia-2026-11-02"> <span class="agenda-dia-nome">Segunda</span> <span[^>]*>02/11/2026</span> '
                     r'<span class="badge rounded-pill status-inativo">Feriado nacional<', html)
    assert html.count("Feriado nacional") == 1


def test_agenda_vazia_mostra_mensagem_clara(client, db, fono):
    logar(client, fono.usuario.email)
    html = _agenda(client)
    assert "Nenhuma consulta nesta semana." in html
    assert "Sem consultas" not in html
    assert "0 consultas" in html


def test_visao_periodo_pela_rota(client, db, fono, paciente):
    criar_plano(db, paciente, fono, [(datetime(2026, 12, 30, 9, 0), AGENDADA), (datetime(2027, 1, 4, 9, 0), AGENDADA)])
    logar(client, fono.usuario.email)
    html = _agenda(client, visao="periodo", inicio="2026-12-28", fim="2027-01-08")
    assert "28/12/2026 a 08/01/2027" in html
    assert "2 consultas" in html
    assert "<details id=\"periodo\" class=\"cartao agenda-filtro mb-4\" open>" in html


def test_periodo_acima_de_92_dias_mostra_mensagem_e_volta_para_a_semana_atual(client, db, fono):
    logar(client, fono.usuario.email)
    html = _agenda(client, visao="periodo", inicio="2026-10-01", fim="2027-01-01")
    assert agenda_service.MSG_PERIODO_LONGO in html
    assert "26/10/2026 a 30/10/2026" in html
    assert 'value="2026-10-01"' in html and 'value="2027-01-01"' in html  # o formulário volta preenchido


@pytest.mark.parametrize("params, mensagem", [
    ({"visao": "periodo", "inicio": "2026-10-10", "fim": "2026-10-01"}, agenda_service.MSG_PERIODO_INVERTIDO),
    ({"visao": "periodo", "inicio": "abc", "fim": "2026-10-01"}, agenda_service.MSG_DATA_INVALIDA),
    ({"visao": "dia", "data": "2026-02-30"}, agenda_service.MSG_DATA_INVALIDA),
])
def test_data_invalida_mostra_mensagem_sem_erro_tecnico(client, db, fono, params, mensagem):
    logar(client, fono.usuario.email)
    resposta = client.get("/agenda", params=params)
    assert resposta.status_code == 200
    assert resposta.headers["content-type"].startswith("text/html")
    html = _html(resposta)
    assert mensagem in html and "Mostrando a semana atual." in html
    assert "26/10/2026 a 30/10/2026" in html


def test_navegacao_mantem_a_visao(client, db, fono):
    logar(client, fono.usuario.email)
    html = _agenda(client, visao="dia", data="2026-10-30")
    assert 'href="/agenda?visao=dia&amp;data=2026-10-29" rel="prev"' in html
    assert 'href="/agenda?visao=dia&amp;data=2026-11-02" rel="next"' in html
    assert 'href="/agenda?visao=dia&amp;data=2026-10-28">Hoje<' in html


def test_tela_nao_adianta_funcionalidades_futuras(client, db, fono, paciente):
    criar_plano(db, paciente, fono, [(datetime(2026, 10, 28, 14, 0), AGENDADA)])
    logar(client, fono.usuario.email)
    html = _agenda(client).lower()
    for texto in ["marcar como realizada", "upload", "enviar avaliação", "remarcar", 'method="post" action="/agenda']:
        assert texto not in html, texto
    assert "<script>" not in re.sub(r'<script src="[^"]+"></script>', "", html)


# --- Acesso ----------------------------------------------------------------------------------------

def test_visitante_vai_para_o_login(client):
    resposta = client.get("/agenda", follow_redirects=False)
    assert resposta.status_code == 303 and resposta.headers["location"] == "/login"


@pytest.mark.parametrize("perfil", ["cliente", "admin"])
def test_cliente_e_admin_sao_bloqueados(client, db, perfil):
    usuario = criar_cliente(db, email="outro@exemplo.com") if perfil == "cliente" else criar_admin(db)
    logar(client, usuario.email)
    assert client.get("/agenda").status_code == 403
    assert client.get("/agenda", params={"visao": "dia", "data": "2026-10-28"}).status_code == 403


def test_profissional_desativado_continua_vendo_a_agenda(client, db, fono, paciente):
    criar_plano(db, paciente, fono, [(datetime(2026, 10, 20, 14, 0), REALIZADA)])
    funcionario_service.desativar(db, fono.usuario.id, AGORA)
    logar(client, fono.usuario.email)
    assert "Pedro Souza" in _agenda(client, visao="semana", data="2026-10-20")


# --- Tela inicial do Profissional ------------------------------------------------------------------

def test_inicio_do_profissional_mostra_consultas_de_hoje(client, db, fono, paciente):
    criar_plano(db, paciente, fono, [(datetime(2026, 10, 28, 9, 0), AGENDADA), (datetime(2026, 10, 28, 14, 0), AGENDADA)])
    criar_plano(db, paciente, fono, [(datetime(2026, 10, 28, 16, 0), PENDENTE)])
    logar(client, fono.usuario.email)
    html = _html(client.get("/"))
    assert re.search(r'Agenda de hoje</h2> <p class="agenda-hoje-numero mb-1"> <span[^>]*>2</span> consultas', html)
    assert 'href="/agenda" class="btn btn-primary' in html and "Ver agenda" in html


def test_inicio_do_profissional_sem_consultas_hoje(client, db, fono):
    logar(client, fono.usuario.email)
    html = _html(client.get("/"))
    assert re.search(r'<span[^>]*>0</span> consultas', html)
    assert "Nenhum atendido marcado para hoje." in html


# --- Agrupamento de dias vazios (função pura) -------------------------------------------------------

CONSULTA = agenda_service.ConsultaAgenda(
    datetime(2026, 10, 1, 9, 0), datetime(2026, 10, 1, 10, 0), "Pedro Souza", 7, "Fonoaudiologia", None, AGENDADA, False,
)
FORA_DA_VISAO = date(2026, 1, 1)


def _dia(texto, com_consulta=False, feriado=False):
    return agenda_service.DiaAgenda(date.fromisoformat(texto), [CONSULTA] if com_consulta else [], feriado)


def _grupo(inicio, fim):
    return agenda_service.DiasSemConsulta(date.fromisoformat(inicio), date.fromisoformat(fim))


def test_um_dia_vazio_isolado_nao_vira_grupo():
    dias = [_dia("2026-10-26", True), _dia("2026-10-27"), _dia("2026-10-28", True)]
    assert agenda_service.agrupar_dias_vazios(dias, FORA_DA_VISAO) == dias


def test_dois_dias_vazios_viram_um_grupo():
    dias = [_dia("2026-11-05"), _dia("2026-11-06")]
    assert agenda_service.agrupar_dias_vazios(dias, FORA_DA_VISAO) == [_grupo("2026-11-05", "2026-11-06")]


def test_grupo_interrompido_por_dia_com_consulta():
    dias = [_dia("2026-10-26"), _dia("2026-10-27"), _dia("2026-10-28", True), _dia("2026-10-29"), _dia("2026-10-30")]
    assert agenda_service.agrupar_dias_vazios(dias, FORA_DA_VISAO) == [
        _grupo("2026-10-26", "2026-10-27"), dias[2], _grupo("2026-10-29", "2026-10-30"),
    ]


def test_hoje_nunca_entra_em_grupo():
    dias = [_dia("2026-10-26"), _dia("2026-10-27"), _dia("2026-10-28"), _dia("2026-10-29"), _dia("2026-10-30")]
    assert agenda_service.agrupar_dias_vazios(dias, HOJE) == [
        _grupo("2026-10-26", "2026-10-27"), dias[2], _grupo("2026-10-29", "2026-10-30"),
    ]


def test_feriado_nunca_entra_em_grupo():
    dias = [_dia("2026-10-29"), _dia("2026-10-30"), _dia("2026-11-02", feriado=True), _dia("2026-11-03"), _dia("2026-11-04", True)]
    assert agenda_service.agrupar_dias_vazios(dias, FORA_DA_VISAO) == [
        _grupo("2026-10-29", "2026-10-30"), dias[2], dias[3], dias[4],
    ]


def test_grupo_atravessa_o_fim_de_semana_nao_exibido():
    visao = Visao(PERIODO, date(2026, 11, 5), date(2026, 11, 10))  # qui a ter; sáb e dom não são exibidos
    dias = agenda_service.agrupar_por_dia(visao, [])
    assert [d.data.weekday() for d in dias] == [3, 4, 0, 1]
    assert agenda_service.agrupar_dias_vazios(dias, FORA_DA_VISAO) == [_grupo("2026-11-05", "2026-11-10")]


def test_periodo_de_92_dias_sem_consulta_vira_um_grupo_so():
    visao = agenda_service.intervalo_da_visao(PERIODO, HOJE, date(2026, 5, 4), date(2026, 8, 3))  # sem feriado nacional
    assert visao.dias == 92
    dias = agenda_service.agrupar_por_dia(visao, [])
    assert agenda_service.agrupar_dias_vazios(dias, FORA_DA_VISAO) == [_grupo("2026-05-04", "2026-08-03")]


def test_semana_mostra_dias_vazios_agrupados(client, db, fono, paciente):
    criar_plano(db, paciente, fono, [(datetime(2026, 10, 26, 9, 0), AGENDADA)])
    logar(client, fono.usuario.email)
    html = _agenda(client)
    assert re.search(r'id="dia-2026-10-27">.*?Sem consultas<', html)  # isolado entre consulta e hoje
    assert re.search(r'agenda-dia-hoje[^"]*" aria-labelledby="dia-2026-10-28"', html)
    assert re.search(r'Sem consultas de <span[^>]*>29/10</span> a <span[^>]*>30/10</span>', html)
    assert 'id="dia-2026-10-29"' not in html and 'id="dia-2026-10-30"' not in html


def test_grupo_na_virada_de_ano_mostra_o_ano(client, db, fono, paciente):
    criar_plano(db, paciente, fono, [(datetime(2028, 1, 5, 9, 0), AGENDADA)])  # 01/01/2028 é sábado: não é exibido
    logar(client, fono.usuario.email)
    html = _agenda(client, visao="periodo", inicio="2027-12-29", fim="2028-01-05")
    assert re.search(r'Sem consultas de <span[^>]*>29/12/2027</span> a <span[^>]*>04/01/2028</span>', html)


def test_grupo_no_mesmo_ano_mostra_so_dia_e_mes(client, db, fono, paciente):
    criar_plano(db, paciente, fono, [(datetime(2026, 12, 28, 9, 0), AGENDADA)])
    logar(client, fono.usuario.email)
    html = _agenda(client, visao="periodo", inicio="2026-12-28", fim="2027-01-08")
    assert re.search(r'Sem consultas de <span[^>]*>29/12</span> a <span[^>]*>31/12</span>', html)
    assert 'id="dia-2027-01-01"' in html  # Confraternização Universal: feriado, fora do grupo
    assert re.search(r'Sem consultas de <span[^>]*>04/01</span> a <span[^>]*>08/01</span>', html)


# --- Visão de dia sem cabeçalho repetido ------------------------------------------------------------

def test_visao_de_dia_hoje_tem_selo_no_titulo_e_cartao_sem_cabecalho(client, db, fono, paciente):
    criar_plano(db, paciente, fono, [(datetime(2026, 10, 28, 14, 0), AGENDADA)])
    logar(client, fono.usuario.email)
    html = _agenda(client, visao="dia", data="2026-10-28")
    assert re.search(r'Quarta, 28/10/2026 <span class="badge rounded-pill status-ativo agenda-selo-titulo">Hoje</span>', html)
    assert "agenda-dia-titulo" not in html
    assert "Pedro Souza" in html


def test_visao_de_outro_dia_nao_tem_selo_hoje(client, db, fono, paciente):
    criar_plano(db, paciente, fono, [(datetime(2026, 10, 29, 14, 0), AGENDADA)])
    logar(client, fono.usuario.email)
    html = _agenda(client, visao="dia", data="2026-10-29")
    assert "Quinta, 29/10/2026" in html
    assert ">Hoje</span>" not in html
    assert "agenda-dia-titulo" not in html and "Pedro Souza" in html


def test_visao_de_dia_feriado_tem_selo_no_titulo(client, db, fono):
    logar(client, fono.usuario.email)
    html = _agenda(client, visao="dia", data="2026-11-02")
    assert re.search(r'Segunda, 02/11/2026 <span class="badge rounded-pill status-inativo agenda-selo-titulo">Feriado nacional<', html)


# --- Polimento de interface: o filtro de período só aparece na aba Período -----------------------

@pytest.mark.parametrize("params", [{}, {"visao": "semana"}, {"visao": "dia", "data": "2026-10-28"}])
def test_filtro_de_periodo_nao_aparece_nas_abas_dia_e_semana(client, fono, params):
    logar(client, fono.usuario.email)
    html = _agenda(client, **params)
    assert 'id="periodo"' not in html and "Escolher um período" not in html
    assert 'href="/agenda?visao=periodo' in html  # a aba Período continua disponível


def test_filtro_de_periodo_aparece_na_aba_periodo_e_no_erro_do_periodo(client, fono):
    logar(client, fono.usuario.email)
    assert "Escolher um período" in _agenda(client, visao="periodo", inicio="2026-10-26", fim="2026-11-06")
    html = _agenda(client, visao="periodo", inicio="2026-10-10", fim="2026-10-01")
    assert "Escolher um período" in html and agenda_service.MSG_PERIODO_INVERTIDO in html
