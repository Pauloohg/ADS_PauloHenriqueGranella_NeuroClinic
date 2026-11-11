import re
from datetime import date, datetime, time, timedelta

import pytest
from sqlalchemy import event

from app.core import config
from app.models.avaliacao import Avaliacao
from app.models.cliente import Cliente
from app.models.sessao import Sessao, StatusSessao
from app.routers import inicio as inicio_router
from app.services import inicio_profissional_service as servico
from app.services.agenda_service import ConsultaAgenda
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

AGORA = datetime(2026, 11, 4, 14, 30)  # quarta-feira, horário local
HOJE = AGORA.date()


@pytest.fixture
def relogio(monkeypatch):
    def ajustar(valor: datetime):
        monkeypatch.setattr(inicio_router, "agora_local", lambda: valor)

    ajustar(AGORA)
    return ajustar


@pytest.fixture
def psicopedagoga(db):
    return criar_funcionario(db, criar_especialidade(db, "Psicopedagogia"), email="edu@exemplo.com", nome="Eduarda Lima Prado")


@pytest.fixture
def outra(db):
    return criar_funcionario(db, criar_especialidade(db, "Psicologia"), email="psi@exemplo.com", nome="Paula Psi")


@pytest.fixture
def cliente(db):
    return criar_cliente(db, nome="Maria Responsável", email="maria.responsavel@exemplo.com")


@pytest.fixture
def pedro(db, cliente):
    return criar_paciente(db, cliente.id, nome="Pedro Souza", nascimento=date(2018, 3, 29))


@pytest.fixture
def lia(db, cliente):
    return criar_paciente(db, cliente.id, nome="Lia Prado", nascimento=date(2018, 1, 10))


@pytest.fixture
def joao(db, cliente):
    return criar_paciente(db, cliente.id, nome="João Lima", nascimento=date(2015, 6, 1))


@pytest.fixture
def logado(client, psicopedagoga, relogio):
    logar(client, psicopedagoga.usuario.email)
    return client


def _html(resposta) -> str:
    assert resposta.status_code == 200
    return " ".join(resposta.text.split())


def _consulta(inicio: datetime, atendido="Pedro Souza", status=AGENDADA) -> ConsultaAgenda:
    return ConsultaAgenda(inicio, inicio + timedelta(hours=1), atendido, 8, "Psicopedagogia", None, status, False)


def _horas_da_linha(html: str) -> list[str]:
    return re.findall(r'class="linha-tempo-hora font-monospace-num">(\d\d:\d\d)<', html)


def _item(html: str, nome: str) -> str:
    itens = html.split('<li class="linha-tempo-item">')[1:]
    return next(trecho.split("</li>")[0] for trecho in itens if nome in trecho.split("</li>")[0])


# --- Saudação ------------------------------------------------------------------------------------

@pytest.mark.parametrize("hora, minuto, esperado", [
    (8, 0, "Bom dia"), (11, 59, "Bom dia"), (12, 0, "Boa tarde"), (17, 59, "Boa tarde"), (18, 0, "Boa noite"),
])
def test_saudacao_por_horario_com_o_primeiro_nome(logado, relogio, hora, minuto, esperado):
    relogio(AGORA.replace(hour=hora, minute=minuto))
    html = _html(logado.get("/"))
    assert re.search(rf'<h1 class="titulo-serifa pagina-titulo">\s*{esperado}, Eduarda\s*</h1>', html)


# --- Subtítulo (função pura) ---------------------------------------------------------------------

def test_subtitulo_sem_consultas_e_sem_proxima():
    assert servico.subtitulo([], None, AGORA) == "Hoje você não tem consultas."


@pytest.mark.parametrize("inicio, esperado", [
    (datetime(2026, 11, 5, 13, 0), "A próxima é amanhã, às 13:00."),
    (datetime(2026, 11, 6, 8, 0), "A próxima é na sexta-feira, 06/11, às 08:00."),
    (datetime(2026, 11, 7, 9, 0), "A próxima é no sábado, 07/11, às 09:00."),
    (datetime(2027, 1, 4, 9, 0), "A próxima é na segunda-feira, 04/01/2027, às 09:00."),
])
def test_subtitulo_sem_consultas_mostra_a_proxima_com_dia_e_hora(inicio, esperado):
    proxima = servico.ConsultaProxima(inicio, "João Lima", 11, "")
    assert servico.subtitulo([], proxima, AGORA) == f"Hoje você não tem consultas. {esperado}"


def test_subtitulo_em_andamento_e_proxima():
    consultas = [_consulta(datetime(2026, 11, 4, 9, 0), "Lia Prado", REALIZADA), _consulta(datetime(2026, 11, 4, 14, 0)),
                 _consulta(datetime(2026, 11, 4, 16, 0), "João Lima")]
    assert servico.subtitulo(consultas, None, AGORA) == (
        "Hoje você tem 3 consultas. A que está em andamento é a de Pedro Souza, e a próxima começa às 16:00."
    )


def test_subtitulo_sem_consulta_em_andamento():
    consultas = [_consulta(datetime(2026, 11, 4, 9, 0)), _consulta(datetime(2026, 11, 4, 16, 0))]
    assert servico.subtitulo(consultas, None, AGORA) == "Hoje você tem 2 consultas. A próxima começa às 16:00."


def test_subtitulo_em_andamento_sem_proxima():
    consultas = [_consulta(datetime(2026, 11, 4, 9, 0)), _consulta(datetime(2026, 11, 4, 14, 0))]
    assert servico.subtitulo(consultas, None, AGORA) == "Hoje você tem 2 consultas. A que está em andamento é a de Pedro Souza."


def test_subtitulo_todas_ja_terminadas():
    consultas = [_consulta(datetime(2026, 11, 4, 9, 0)), _consulta(datetime(2026, 11, 4, 13, 30))]
    assert servico.subtitulo(consultas, None, AGORA) == "Hoje você teve 2 consultas, todas já aconteceram."


@pytest.mark.parametrize("inicio, esperado", [
    (datetime(2026, 11, 4, 14, 0), "Hoje você tem 1 consulta, a de Pedro Souza, que está em andamento."),
    (datetime(2026, 11, 4, 16, 0), "Hoje você tem 1 consulta, que começa às 16:00."),
    (datetime(2026, 11, 4, 9, 0), "Hoje você teve 1 consulta, que já aconteceu."),
])
def test_subtitulo_no_singular(inicio, esperado):
    assert servico.subtitulo([_consulta(inicio)], None, AGORA) == esperado


def test_subtitulo_aparece_na_pagina(db, logado, psicopedagoga, pedro, lia, joao):
    criar_plano(db, lia, psicopedagoga, [(datetime(2026, 11, 4, 9, 0), REALIZADA)])
    criar_plano(db, pedro, psicopedagoga, [(datetime(2026, 11, 4, 14, 0), AGENDADA)])
    criar_plano(db, joao, psicopedagoga, [(datetime(2026, 11, 4, 16, 0), AGENDADA)])
    criar_plano(db, joao, psicopedagoga, [(datetime(2026, 11, 4, 10, 0), PENDENTE)])
    html = _html(logado.get("/"))
    assert ('Hoje você tem 3 consultas. A que está em andamento é a de Pedro Souza, e a próxima começa às 16:00.'
            in html)


# --- Em andamento (função pura, limites) -----------------------------------------------------------

@pytest.mark.parametrize("agora, esperado", [
    (datetime(2026, 11, 4, 13, 59), False),
    (datetime(2026, 11, 4, 14, 0), True),
    (datetime(2026, 11, 4, 14, 59), True),
    (datetime(2026, 11, 4, 15, 0), False),
])
def test_em_andamento_nos_limites(agora, esperado):
    assert servico.em_andamento(_consulta(datetime(2026, 11, 4, 14, 0)), agora) is esperado


# --- Grade da linha do tempo -------------------------------------------------------------------------

def test_grade_sai_da_configuracao(logado):
    html = _html(logado.get("/"))
    assert _horas_da_linha(html) == [f"{hora:02d}:00" for hora in config.HORARIOS_ATENDIMENTO]
    assert html.count("Livre") == len(config.HORARIOS_ATENDIMENTO)
    assert '<li class="linha-tempo-intervalo">12:00 às 14:00, sem atendimento</li>' in html


def test_grade_diferente_na_configuracao_muda_as_linhas_e_o_intervalo(logado, monkeypatch):
    monkeypatch.setattr(config, "HORARIOS_ATENDIMENTO", [8, 9, 10, 14, 15, 16])
    html = _html(logado.get("/"))
    assert _horas_da_linha(html) == ["08:00", "09:00", "10:00", "14:00", "15:00", "16:00"]
    assert "11:00" not in _horas_da_linha(html) and "17:00" not in _horas_da_linha(html)
    assert '<li class="linha-tempo-intervalo">11:00 às 14:00, sem atendimento</li>' in html


def test_linha_do_tempo_poe_sessao_fora_da_grade_na_posicao_certa():
    consultas = [_consulta(datetime(2026, 11, 4, 7, 0), "Antes"), _consulta(datetime(2026, 11, 4, 12, 30), "Intervalo")]
    itens = servico.linha_do_tempo(consultas, AGORA, [8, 9, 14])
    rotulos = [item.intervalo or f"{item.horario:%H:%M}" for item in itens]
    assert rotulos == ["07:00", "08:00", "09:00", "10:00 às 14:00, sem atendimento", "12:30", "14:00"]
    assert [d.consulta.atendido for d in itens[0].consultas] == ["Antes"]
    assert [d.consulta.atendido for d in itens[4].consultas] == ["Intervalo"]


def test_grade_sem_intervalo_nao_mostra_linha_tracejada():
    itens = servico.linha_do_tempo([], AGORA, [8, 9, 10])
    assert [item.horario for item in itens] == [time(8), time(9), time(10)]
    assert all(item.intervalo is None for item in itens)


def test_sessao_fora_da_grade_aparece_na_pagina(db, logado, psicopedagoga, pedro):
    criar_plano(db, pedro, psicopedagoga, [(datetime(2026, 11, 4, 12, 0), REALIZADA)])
    horas = _horas_da_linha(_html(logado.get("/")))
    assert horas.index("12:00") == horas.index("11:00") + 1


# --- Consultas na linha do tempo ------------------------------------------------------------------

def _com_observacao(db, plano, texto):
    plano.observacao = texto
    db.commit()
    return plano


def test_observacao_so_na_consulta_em_andamento_e_escapada(db, logado, psicopedagoga, pedro, joao):
    _com_observacao(db, criar_plano(db, pedro, psicopedagoga, [(datetime(2026, 11, 4, 14, 0), AGENDADA)]),
                    "<script>alert(1)</script> prefere música")
    _com_observacao(db, criar_plano(db, joao, psicopedagoga, [(datetime(2026, 11, 4, 16, 0), AGENDADA)]),
                    "Observação do João")
    html = _html(logado.get("/"))
    em_andamento = _item(html, "Pedro Souza")
    assert "linha-tempo-agora" in em_andamento and "Em andamento" in em_andamento
    assert "Observação do plano: <span" in em_andamento
    assert "&lt;script&gt;alert(1)&lt;/script&gt; prefere música" in em_andamento
    assert "<script>alert" not in html
    assert "Observação do João" not in html
    assert html.count("Observação do plano") == 1


def test_em_andamento_tem_texto_e_so_uma_consulta_ganha_o_bloco(db, logado, psicopedagoga, pedro, joao):
    criar_plano(db, pedro, psicopedagoga, [(datetime(2026, 11, 4, 14, 0), AGENDADA)])
    criar_plano(db, joao, psicopedagoga, [(datetime(2026, 11, 4, 16, 0), AGENDADA)])
    html = _html(logado.get("/"))
    assert html.count("linha-tempo-agora") == 1
    assert '<span class="badge rounded-pill selo-em-andamento">Em andamento</span>' in _item(html, "Pedro Souza")
    assert "status-ativo\">Agendada" in _item(html, "João Lima")


def test_registrar_so_em_sessao_encerrada_nao_registrada(db, logado, psicopedagoga, pedro, lia, joao):
    encerrada = criar_plano(db, pedro, psicopedagoga, [(datetime(2026, 11, 4, 8, 0), AGENDADA)])
    criar_plano(db, lia, psicopedagoga, [(datetime(2026, 11, 4, 9, 0), REALIZADA)])
    criar_plano(db, joao, psicopedagoga, [(datetime(2026, 11, 4, 13, 30), AGENDADA)])  # termina às 14:30 = agora
    criar_plano(db, joao, psicopedagoga, [(datetime(2026, 11, 4, 16, 0), AGENDADA)])
    sessao_id = db.query(Sessao.id).filter(Sessao.plano_id == encerrada.id).scalar()
    html = _html(logado.get("/"))
    item = _item(html, "Pedro Souza")
    assert '<span class="badge rounded-pill status-pendente">Aguardando registro</span>' in item
    assert f'href="/agenda/sessoes/{sessao_id}/registrar"' in item
    assert html.count('/registrar" class="btn') == 2  # Pedro (8h) e João (13:30, termina exatamente agora)
    assert "Aguardando registro" not in _item(html, "Lia Prado") and "/registrar" not in _item(html, "Lia Prado")
    assert "Agendada" in _item(html, "16:00")


def test_realizada_com_nao_comparecimento_e_remarcada(db, logado, psicopedagoga, pedro):
    plano = criar_plano(db, pedro, psicopedagoga, [(datetime(2026, 11, 4, 9, 0), REALIZADA)])
    sessao = db.query(Sessao).filter(Sessao.plano_id == plano.id).one()
    sessao.foi_remarcada = True
    db.add(Avaliacao(sessao_id=sessao.id, comparecimento=False, liberado_em=datetime(2026, 11, 4, 13, 0)))
    db.commit()
    item = _item(_html(logado.get("/")), "Pedro Souza")
    assert "status-inativo\">Realizada" in item
    assert "status-nao-compareceu\">Não compareceu" in item
    assert "status-remarcada\">Remarcada" in item


def test_titulo_do_dia_e_link_da_agenda(logado):
    html = _html(logado.get("/"))
    assert "Quarta-feira, 4 de novembro" in html
    assert '<a href="/agenda" class="inicio-prof-link">Ver agenda da semana</a>' in html


def test_linha_do_tempo_e_lista_semantica(logado):
    assert '<ol class="linha-tempo" aria-label="Consultas de hoje, por horário">' in _html(logado.get("/"))


@pytest.mark.parametrize("dia, esperado", [
    (date(2026, 11, 2), "Segunda-feira, 2 de novembro"),
    (date(2026, 11, 7), "Sábado, 7 de novembro"),
    (date(2027, 1, 1), "Sexta-feira, 1 de janeiro"),
])
def test_titulo_do_dia(dia, esperado):
    assert servico.titulo_do_dia(dia) == esperado


# --- Fim de semana e feriado --------------------------------------------------------------------

def test_fim_de_semana_sem_consultas_mostra_mensagem(logado, relogio):
    relogio(datetime(2026, 11, 7, 10, 0))
    html = _html(logado.get("/"))
    assert "Hoje não há atendimento: fim de semana." in html
    assert "Livre" not in html and "linha-tempo-item" not in html
    assert "Sábado, 7 de novembro" in html


def test_feriado_sem_consultas_mostra_o_nome(logado, relogio):
    relogio(datetime(2026, 9, 7, 10, 0))
    html = _html(logado.get("/"))
    assert "Hoje é feriado nacional: Independência do Brasil." in html
    assert "Livre" not in html and "Feriado nacional</span>" in html


def test_feriado_com_consulta_mostra_a_consulta(db, logado, relogio, psicopedagoga, pedro):
    relogio(datetime(2026, 9, 7, 10, 0))
    criar_plano(db, pedro, psicopedagoga, [(datetime(2026, 9, 7, 14, 0), AGENDADA)])
    html = _html(logado.get("/"))
    assert "Hoje é feriado nacional" not in html and "Livre" not in html
    assert _horas_da_linha(html) == ["14:00"] and "Pedro Souza" in html


def test_feriado_no_fim_de_semana_prefere_o_nome_do_feriado():
    assert servico.motivo_sem_atendimento(date(2026, 11, 15)) == "Hoje é feriado nacional: Proclamação da República."
    assert servico.motivo_sem_atendimento(date(2026, 11, 8)) == "Hoje não há atendimento: fim de semana."
    assert servico.motivo_sem_atendimento(HOJE) is None


# --- Nos próximos dias ----------------------------------------------------------------------------

def test_proximos_dias_limite_ordem_e_filtros(db, logado, psicopedagoga, outra, pedro, lia, joao):
    criar_plano(db, pedro, psicopedagoga, [
        (datetime(2026, 11, 4, 16, 0), AGENDADA),  # hoje: fica de fora
        (datetime(2026, 11, 5, 13, 0), AGENDADA),
        (datetime(2026, 11, 9, 8, 0), AGENDADA),
        (datetime(2026, 11, 10, 8, 0), AGENDADA),
        (datetime(2026, 11, 11, 8, 0), AGENDADA),
        (datetime(2026, 11, 12, 8, 0), AGENDADA),
        (datetime(2026, 11, 13, 8, 0), AGENDADA),  # sexta da semana seguinte: além do limite de 5
    ])
    criar_plano(db, lia, psicopedagoga, [(datetime(2026, 11, 5, 11, 0), AGENDADA)])
    criar_plano(db, joao, psicopedagoga, [(datetime(2026, 11, 5, 8, 0), PENDENTE)])
    criar_plano(db, joao, outra, [(datetime(2026, 11, 5, 9, 0), AGENDADA)])
    proximas = servico.proximas_consultas(db, psicopedagoga.usuario.id, HOJE)
    assert [(p.inicio, p.atendido) for p in proximas] == [
        (datetime(2026, 11, 5, 11, 0), "Lia Prado"),
        (datetime(2026, 11, 5, 13, 0), "Pedro Souza"),
        (datetime(2026, 11, 9, 8, 0), "Pedro Souza"),
        (datetime(2026, 11, 10, 8, 0), "Pedro Souza"),
        (datetime(2026, 11, 11, 8, 0), "Pedro Souza"),
    ]
    html = _html(logado.get("/"))
    lista = html.split('class="inicio-prof-proximos-lista">')[1].split("</ol>")[0]
    assert lista.count("<li>") == 5
    assert "11:00</strong> <span class=\"font-monospace-num\">qui, 05/11</span>" in lista
    assert "seg, 09/11" in lista and "13/11" not in lista and "João Lima" not in lista
    assert "Lia Prado</strong> <span class=\"linha-tempo-idade\">8 anos</span>" in lista


def test_proximos_dias_vazio(logado):
    assert "Nenhuma consulta agendada nos próximos dias." in _html(logado.get("/"))


def test_proximos_dias_ignora_realizada_futura_e_agendada_de_ontem(db, psicopedagoga, pedro):
    criar_plano(db, pedro, psicopedagoga, [(datetime(2026, 11, 3, 9, 0), AGENDADA), (datetime(2026, 11, 6, 9, 0), REALIZADA)])
    assert servico.proximas_consultas(db, psicopedagoga.usuario.id, HOJE) == []


@pytest.mark.parametrize("dia, esperado", [
    (date(2026, 11, 5), "qui, 05/11"), (date(2026, 11, 9), "seg, 09/11"), (date(2026, 11, 7), "sáb, 07/11"),
    (date(2027, 1, 4), "seg, 04/01/2027"),
])
def test_dia_abreviado(dia, esperado):
    assert servico.dia_abreviado(dia, HOJE) == esperado


# --- Aviso de consultas esperando registro --------------------------------------------------------

def _aviso(html: str) -> str:
    return html.split('<section class="inicio-prof-aviso"')[1].split("</section>")[0]


def test_aviso_de_registro_no_plural_com_links(db, logado, psicopedagoga, pedro):
    criar_plano(db, pedro, psicopedagoga, [
        (datetime(2026, 11, 2, 9, 0), AGENDADA), (datetime(2026, 11, 3, 9, 0), AGENDADA), (datetime(2026, 11, 4, 8, 0), AGENDADA),
        (datetime(2026, 11, 4, 16, 0), AGENDADA),
    ])
    aviso = _aviso(_html(logado.get("/")))
    assert '<span class="font-monospace-num">3</span> consultas esperando registro' in aviso
    assert "Essas consultas já aconteceram e ainda não foram registradas." in aviso
    assert '<a href="/agenda?visao=registrar" class="btn btn-primary btn-sm">Registrar agora</a>' in aviso


def test_aviso_de_registro_no_singular(db, logado, psicopedagoga, pedro):
    criar_plano(db, pedro, psicopedagoga, [(datetime(2026, 11, 3, 9, 0), AGENDADA)])
    aviso = _aviso(_html(logado.get("/")))
    assert '<span class="font-monospace-num">1</span> consulta esperando registro' in aviso
    assert "Essa consulta já aconteceu e ainda não foi registrada." in aviso


def test_aviso_de_registro_oculto_com_zero(db, logado, psicopedagoga, pedro):
    criar_plano(db, pedro, psicopedagoga, [(datetime(2026, 11, 4, 16, 0), AGENDADA), (datetime(2026, 11, 3, 9, 0), REALIZADA)])
    html = _html(logado.get("/"))
    assert "esperando registro" not in html and "Registrar agora" not in html
    assert 'class="inicio-prof inicio-prof-sem-aviso"' in html


def test_aviso_usa_a_mesma_contagem_do_rf09(db, psicopedagoga, outra, pedro):
    from app.services import avaliacao_service

    criar_plano(db, pedro, psicopedagoga, [(datetime(2026, 11, 3, 9, 0), AGENDADA), (datetime(2026, 11, 4, 13, 30), AGENDADA)])
    criar_plano(db, pedro, outra, [(datetime(2026, 11, 3, 9, 0), AGENDADA)])
    especialidade, total = servico.especialidade_e_a_registrar(db, psicopedagoga.usuario.id, AGORA)
    assert especialidade == "Psicopedagogia"
    assert total == avaliacao_service.contar_a_registrar(db, psicopedagoga.usuario.id, AGORA) == 2


def test_no_celular_o_aviso_vem_logo_depois_da_saudacao(db, logado, psicopedagoga, pedro):
    criar_plano(db, pedro, psicopedagoga, [(datetime(2026, 11, 3, 9, 0), AGENDADA)])
    html = _html(logado.get("/"))
    assert html.index("</h1>") < html.index('class="inicio-prof-aviso"') < html.index('class="cartao inicio-prof-dia"')


def test_selo_da_especialidade_no_topo(logado):
    html = _html(logado.get("/"))
    assert '<div class="pagina-acoes"> <span class="chip-especialidade esp-psicopedagogia">Psicopedagogia</span> </div>' in html


# --- Privacidade (RNF09) -------------------------------------------------------------------------

def test_privacidade_so_nome_e_idade_do_atendido(db, logado, psicopedagoga, pedro, cliente):
    db.get(Cliente, cliente.id).cpf = "12345678909"
    db.commit()
    criar_plano(db, pedro, psicopedagoga, [(datetime(2026, 11, 4, 14, 0), AGENDADA), (datetime(2026, 11, 5, 9, 0), AGENDADA)],
                valor_total="987.65")
    html = _html(logado.get("/"))
    assert "Pedro Souza" in html and "8 anos" in html
    for proibido in ("Maria Responsável", "maria.responsavel@exemplo.com", "99999-1234", "12345678909", "123.456.789-09",
                     "29/03/2018", "2018-03-29", "987,65", "R$", "Pagamento", "pagamento"):
        assert proibido not in html, proibido


# --- Consultas SQL -------------------------------------------------------------------------------

def _contar_comandos(db, funcao):
    comandos = []
    ouvinte = lambda *args: comandos.append(args[2])  # noqa: E731
    event.listen(db.get_bind(), "before_cursor_execute", ouvinte)
    try:
        resultado = funcao()
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", ouvinte)
    return resultado, comandos


@pytest.mark.parametrize("quantidade", [1, 40])
def test_no_maximo_3_consultas_sql(db, client, relogio, psicopedagoga, cliente, quantidade):
    for indice in range(quantidade):
        paciente = criar_paciente(db, cliente.id, nome=f"Atendido {indice}")
        criar_plano(db, paciente, psicopedagoga, [
            (datetime(2026, 11, 4, 8, 0) + timedelta(hours=indice % 3), AGENDADA),
            (datetime(2026, 9, 1, 8, 0) + timedelta(days=indice), AGENDADA),
            (datetime(2026, 11, 5, 8, 0) + timedelta(days=indice), AGENDADA),
        ])
    funcionario_id = psicopedagoga.usuario.id
    db.expire_all()
    inicio, comandos = _contar_comandos(db, lambda: servico.montar_inicio(db, funcionario_id, AGORA))
    assert len(comandos) <= 3
    assert inicio.a_registrar == 2 * quantidade  # as de setembro e as de hoje (8h a 10h, já terminadas)
    assert sum(len(item.consultas) for item in inicio.linha_do_tempo) == quantidade
    assert len(inicio.proximas) == min(quantidade, 5)

    logar(client, psicopedagoga.usuario.email)
    db.expire_all()
    resposta, comandos = _contar_comandos(db, lambda: client.get("/"))
    assert resposta.status_code == 200
    assert len(comandos) <= 4  # 1 da autenticação (busca do usuário) + 3 da página


# --- As outras telas iniciais não mudaram -----------------------------------------------------------

def test_inicio_do_cliente_e_do_admin_nao_usam_o_layout_novo(db, client, relogio, cliente):
    logar(client, cliente.email)
    html_cliente = _html(client.get("/"))
    assert "Primeiros passos" in html_cliente
    client.cookies.clear()
    logar(client, criar_admin(db).email)
    html_admin = _html(client.get("/"))
    assert 'id="titulo-cadastros"' in html_admin and "<strong>Profissionais</strong>" in html_admin
    for html in (html_cliente, html_admin):
        assert "inicio-prof" not in html and "linha-tempo" not in html and "esperando registro" not in html
