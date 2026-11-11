import re
from dataclasses import replace
from datetime import date, datetime

import pytest
from sqlalchemy import event, select

from app.core import config
from app.core.ids import ler_data_iso
from app.models.avaliacao import Avaliacao
from app.models.cliente import Cliente
from app.models.sessao import Sessao, StatusSessao
from app.routers import agenda as agenda_router
from app.services import agenda_service as servico
from app.services import inicio_profissional_service
from app.services.agenda_service import ConsultaAgenda
from tests.fabricas import criar_cliente, criar_especialidade, criar_funcionario, criar_paciente, criar_plano, logar

AGENDADA = StatusSessao.AGENDADA
REALIZADA = StatusSessao.REALIZADA
PENDENTE = StatusSessao.PENDENTE_PAGAMENTO

AGORA = datetime(2026, 11, 5, 14, 30)  # quinta-feira, horário local
HOJE = AGORA.date()


@pytest.fixture(autouse=True)
def relogio_fixo(monkeypatch):
    monkeypatch.setattr(agenda_router, "agora_local", lambda: AGORA)


@pytest.fixture
def prof(db):
    return criar_funcionario(db, criar_especialidade(db, "Psicopedagogia"), email="carla@exemplo.com", nome="Carla")


@pytest.fixture
def cliente(db):
    return criar_cliente(db, nome="Maria Responsável")


@pytest.fixture
def pedro(db, cliente):
    return criar_paciente(db, cliente.id, nome="Pedro Souza", nascimento=date(2019, 3, 1))


@pytest.fixture
def logado(client, prof):
    logar(client, prof.usuario.email)
    return client


def _html(resposta) -> str:
    assert resposta.status_code == 200
    return " ".join(resposta.text.split())


def _agenda(client, **params) -> str:
    return _html(client.get("/agenda", params=params))


def _consulta(inicio, status=AGENDADA, avaliacao_id=None, comparecimento=None, tem_arquivo=False, remarcada=False):
    return ConsultaAgenda(
        inicio, inicio + servico.DURACAO_SESSAO, "Pedro Souza", 7, "Psicopedagogia", None, status, remarcada,
        sessao_id=1, avaliacao_id=avaliacao_id, comparecimento=comparecimento, tem_arquivo=tem_arquivo,
    )


def _avaliar(db, data_hora, comparecimento, arquivo=None):
    sessao = db.scalar(select(Sessao).where(Sessao.data_hora == data_hora))
    db.add(Avaliacao(sessao_id=sessao.id, comparecimento=comparecimento, arquivo_url=arquivo,
                     liberado_em=datetime(2026, 11, 1, 12, 0)))
    db.commit()


# --- Estado único da consulta (função pura) --------------------------------------------------------

@pytest.mark.parametrize("consulta, esperado", [
    (_consulta(datetime(2026, 11, 5, 16, 0)), servico.AGENDADA),
    (_consulta(datetime(2026, 11, 5, 14, 0)), servico.EM_ANDAMENTO),
    (_consulta(datetime(2026, 11, 5, 9, 0)), servico.AGUARDANDO_REGISTRO),
    (_consulta(datetime(2026, 11, 3, 9, 0), REALIZADA, 1, True, True), servico.AVALIACAO_ENVIADA),
    (_consulta(datetime(2026, 11, 4, 10, 0), REALIZADA, 2, False), servico.NAO_COMPARECEU),
    (_consulta(datetime(2026, 10, 1, 9, 0), REALIZADA), servico.REALIZADA),  # realizada sem avaliação (dado antigo)
    (_consulta(datetime(2026, 10, 1, 9, 0), REALIZADA, 3, True, False), servico.REALIZADA),  # sem arquivo
], ids=["agendada", "em_andamento", "aguardando_registro", "avaliacao_enviada", "nao_compareceu",
        "realizada_sem_avaliacao", "realizada_sem_arquivo"])
def test_estado_unico_da_consulta(consulta, esperado):
    assert servico.estado_da_consulta(consulta, AGORA) == esperado


@pytest.mark.parametrize("agora, esperado", [
    (datetime(2026, 11, 5, 13, 59), servico.AGENDADA),
    (datetime(2026, 11, 5, 14, 0), servico.EM_ANDAMENTO),
    (datetime(2026, 11, 5, 14, 59), servico.EM_ANDAMENTO),
    (datetime(2026, 11, 5, 15, 0), servico.AGUARDANDO_REGISTRO),  # exatamente no término já pode registrar
])
def test_estado_nos_limites_do_em_andamento(agora, esperado):
    assert servico.estado_da_consulta(_consulta(datetime(2026, 11, 5, 14, 0)), agora) == esperado


def test_em_andamento_e_a_mesma_funcao_da_tela_inicial():
    assert inicio_profissional_service.em_andamento is servico.em_andamento


def test_rota_mostra_um_so_selo_de_estado_por_consulta(db, logado, prof, pedro):
    criar_plano(db, pedro, prof, [
        (datetime(2026, 11, 3, 9, 0), REALIZADA), (datetime(2026, 11, 4, 10, 0), REALIZADA),
        (datetime(2026, 11, 5, 9, 0), AGENDADA), (datetime(2026, 11, 5, 14, 0), AGENDADA),
        (datetime(2026, 11, 6, 9, 0), AGENDADA), (datetime(2026, 11, 2, 9, 0), REALIZADA),
    ])
    _avaliar(db, datetime(2026, 11, 3, 9, 0), True, "0" * 32 + ".pdf")
    _avaliar(db, datetime(2026, 11, 4, 10, 0), False)
    html = _agenda(logado)
    linhas = re.findall(r'<li class="agenda-linha agenda-estado agenda-estado-([\w-]+)">(.*?)</li>', html)
    estados = [estado for estado, _ in linhas]
    assert estados == ["realizada", "avaliacao-enviada", "nao-compareceu", "aguardando-registro", "em-andamento", "agendada"]
    rotulos = {"agendada": "Agendada", "em-andamento": "Em andamento", "aguardando-registro": "Aguardando registro",
               "avaliacao-enviada": "Avaliação enviada", "nao-compareceu": "Não compareceu", "realizada": "Realizada"}
    for estado, corpo in linhas:
        assert len(re.findall(r'class="badge rounded-pill [\w-]+">', corpo)) == 1, estado
        assert f">{rotulos[estado]}</span>" in corpo
    assert html.count("Registrar atendimento<span") == 1 and html.count("Ver avaliação<span") == 1


def test_remarcada_e_o_selo_extra(db, logado, prof, pedro):
    plano = criar_plano(db, pedro, prof, [(datetime(2026, 11, 6, 9, 0), AGENDADA)])
    db.scalar(select(Sessao).where(Sessao.plano_id == plano.id)).foi_remarcada = True
    db.commit()
    html = _agenda(logado)
    assert re.search(r'agenda-selo-agendada">Agendada</span> <span class="badge rounded-pill status-remarcada">Remarcada<', html)


def test_sem_especialidade_nas_telas_da_agenda(db, logado, prof, pedro):
    criar_plano(db, pedro, prof, [(datetime(2026, 11, 5, 9, 0), AGENDADA)])
    for params in ({}, {"visao": "dia", "data": "2026-11-05"}, {"visao": "periodo", "inicio": "2026-11-02", "fim": "2026-11-06"},
                   {"visao": "registrar"}):
        html = _agenda(logado, **params)
        assert "Psicopedagogia" not in html and "chip-especialidade" not in html and "esp-psicopedagogia" not in html


# --- Resumo e textos relativos (funções puras) -----------------------------------------------------

def _com_estado(*consultas):
    return [replace(c, estado=servico.estado_da_consulta(c, AGORA)) for c in consultas]


@pytest.mark.parametrize("consultas, esperado", [
    ([], "Nenhuma consulta"),
    (_com_estado(_consulta(datetime(2026, 11, 5, 16, 0))), "1 consulta"),
    (_com_estado(_consulta(datetime(2026, 11, 5, 14, 0))), "1 consulta, 1 em andamento"),
    (_com_estado(_consulta(datetime(2026, 11, 5, 9, 0)), _consulta(datetime(2026, 11, 5, 14, 0)),
                 _consulta(datetime(2026, 11, 5, 16, 0))), "3 consultas, 1 em andamento, 1 aguardando registro"),
    (_com_estado(_consulta(datetime(2026, 11, 3, 9, 0)), _consulta(datetime(2026, 11, 4, 9, 0))),
     "2 consultas, 2 aguardando registro"),
])
def test_resumo_das_consultas(consultas, esperado):
    assert servico.resumo_das_consultas(consultas) == esperado


@pytest.mark.parametrize("momento, texto, destacado", [
    (datetime(2026, 11, 5, 9, 0), "hoje", False),
    (datetime(2026, 11, 4, 23, 0), "ontem", False),  # dias de calendário, não 24 h
    (datetime(2026, 11, 3, 16, 0), "há 2 dias", False),
    (datetime(2026, 10, 23, 9, 0), "há 13 dias", False),
    (datetime(2026, 10, 22, 9, 0), "há 14 dias", True),
    (datetime(2026, 10, 14, 9, 0), "há 22 dias", True),
])
def test_ha_quanto_tempo_e_destaque_de_14_dias(momento, texto, destacado):
    assert servico.ha_quanto_tempo(momento, AGORA) == texto
    assert servico.atraso_destacado(momento, AGORA) is destacado


# --- Semana -----------------------------------------------------------------------------------------

def test_observacao_aparece_em_toda_consulta_que_a_tenha(db, logado, prof, pedro, cliente):
    plano = criar_plano(db, pedro, prof, [(datetime(2026, 11, 3, 9, 0), AGENDADA), (datetime(2026, 11, 6, 9, 0), AGENDADA)])
    plano.observacao = "Trazer o caderno."
    criar_plano(db, criar_paciente(db, cliente.id, nome="Ana Lima"), prof, [(datetime(2026, 11, 6, 10, 0), AGENDADA)])
    db.commit()
    html = _agenda(logado)
    assert html.count("<strong>Observação do plano:</strong>") == 2
    periodo = _agenda(logado, visao="periodo", inicio="2026-11-02", fim="2026-11-06")
    assert "Observação do plano" not in periodo  # no Período a observação fica na visão Dia
    dia = _agenda(logado, visao="dia", data="2026-11-06")
    assert dia.count("Observação do plano:") == 1 and dia.count("Sem observação do plano.") == 1


def test_semana_feriado_vira_linha_curta_e_dia_e_link(db, logado, prof, pedro):
    criar_plano(db, pedro, prof, [(datetime(2026, 11, 3, 9, 0), AGENDADA)])
    html = _agenda(logado, visao="semana", data="2026-11-02")
    assert re.search(r'id="dia-2026-11-02"> <a class="agenda-link-dia" href="/agenda\?visao=dia&amp;data=2026-11-02">'
                     r'Segunda, 02/11</a> <span class="badge rounded-pill status-inativo">Feriado nacional</span>', html)
    assert "Sem atendimento: Finados." in html
    assert '<p class="agenda-resumo">1 consulta, 1 aguardando registro.</p>' in html


def test_semana_destaca_hoje_e_subtitulo(db, logado, prof, pedro):
    criar_plano(db, pedro, prof, [(datetime(2026, 11, 5, 16, 0), AGENDADA)])
    html = _agenda(logado)
    assert "Consultas confirmadas." in html and "aguardando pagamento" not in html
    assert html.count("agenda-dia-hoje") == 1
    assert re.search(r'agenda-dia-hoje[^"]*" aria-labelledby="dia-2026-11-05"> <h3[^>]*> <a[^>]*>Quinta, 05/11</a> '
                     r'<span class="badge rounded-pill status-ativo">Hoje</span>', html)


def test_aba_a_registrar_separada_e_contagem(db, logado, prof, pedro):
    html = _agenda(logado)
    assert re.search(r'>Período</a> <span class="agenda-abas-separador" aria-hidden="true"></span> <a href="/agenda\?visao=registrar"', html)
    assert "agenda-contador-registrar" not in html  # some com zero

    criar_plano(db, pedro, prof, [(datetime(2026, 11, 3, 9, 0), AGENDADA), (datetime(2026, 11, 4, 9, 0), AGENDADA),
                                  (datetime(2026, 11, 5, 14, 0), AGENDADA)])  # a terceira está em andamento
    for params in ({}, {"visao": "dia"}, {"visao": "periodo", "inicio": "2026-11-01", "fim": "2026-11-30"}, {"visao": "registrar"}):
        assert '<span class="badge rounded-pill agenda-contador-registrar">2</span>' in _agenda(logado, **params)


# --- Dia ----------------------------------------------------------------------------------------------

def test_dia_cabecalho_resumo_cartoes_e_turnos(db, logado, prof, pedro, cliente):
    criar_plano(db, pedro, prof, [(datetime(2026, 11, 5, 14, 0), AGENDADA)])
    criar_plano(db, criar_paciente(db, cliente.id, nome="Ana Lima"), prof, [(datetime(2026, 11, 5, 9, 0), AGENDADA)])
    criar_plano(db, criar_paciente(db, cliente.id, nome="Luís Rocha"), prof, [(datetime(2026, 11, 5, 16, 0), AGENDADA)])
    html = _agenda(logado, visao="dia", data="2026-11-05")
    assert re.search(r'Quinta, 05/11/2026 <span class="badge rounded-pill status-ativo agenda-selo-titulo">Hoje</span>', html)
    assert '<p class="agenda-resumo">3 consultas, 1 em andamento, 1 aguardando registro.</p>' in html
    manha, tarde = html.index(">Manhã</h3>"), html.index(">Tarde</h3>")
    faixa = html.index("12:00 às 14:00, sem atendimento")
    assert manha < html.index("Ana Lima") < faixa < tarde < html.index("Pedro Souza") < html.index("Luís Rocha")
    assert html.count("agenda-cartao-andamento") == 1
    assert re.search(r'agenda-cartao-hora mb-0"><span class="font-monospace-num">14:00</span><span class="agenda-cartao-ate">até '
                     r'<span class="font-monospace-num">15:00</span>', html)


def test_dia_so_com_tarde_nao_mostra_a_faixa(db, logado, prof, pedro):
    criar_plano(db, pedro, prof, [(datetime(2026, 11, 6, 14, 0), AGENDADA)])
    html = _agenda(logado, visao="dia", data="2026-11-06")
    assert ">Tarde</h3>" in html and ">Manhã</h3>" not in html and "sem atendimento" not in html


def test_faixa_do_dia_sai_da_configuracao(db, logado, prof, pedro, monkeypatch):
    monkeypatch.setattr(config, "HORARIOS_ATENDIMENTO", [8, 9, 10, 14, 15])
    criar_plano(db, pedro, prof, [(datetime(2026, 11, 6, 9, 0), AGENDADA), (datetime(2026, 11, 6, 15, 0), AGENDADA)])
    html = _agenda(logado, visao="dia", data="2026-11-06")
    assert '<p class="agenda-faixa-livre">11:00 às 14:00, sem atendimento</p>' in html
    assert "12:00 às 14:00" not in html


def test_separar_turnos_e_faixas_puras():
    manha, tarde = _consulta(datetime(2026, 11, 6, 11, 0)), _consulta(datetime(2026, 11, 6, 12, 0))
    turnos = servico.separar_turnos([manha, tarde], [8, 9, 10, 11, 14, 15, 16, 17])
    assert turnos.manha == [manha] and turnos.tarde == [tarde]
    assert [f.texto for f in turnos.faixas] == ["12:00 às 14:00, sem atendimento"]
    assert servico.faixas_sem_atendimento([8, 9, 10]) == []
    assert [f.texto for f in servico.faixas_sem_atendimento([8, 10, 14])] == [
        "09:00 às 10:00, sem atendimento", "11:00 às 14:00, sem atendimento",
    ]
    assert [f.texto for f in servico.separar_turnos([], [8, 10, 14]).faixas] == ["11:00 às 14:00, sem atendimento"]


@pytest.mark.parametrize("data, anterior, proximo", [
    ("2026-11-06", "2026-11-05", "2026-11-09"),  # sexta: próximo dia útil pula o fim de semana
    ("2026-11-09", "2026-11-06", "2026-11-10"),  # segunda: o anterior pula o fim de semana
    ("2026-10-30", "2026-10-29", "2026-11-02"),  # o próximo cai no feriado de Finados, que continua acessível
    ("2026-11-07", "2026-11-06", "2026-11-09"),  # sábado digitado na URL
])
def test_navegacao_do_dia(logado, data, anterior, proximo):
    html = _agenda(logado, visao="dia", data=data)
    assert f'href="/agenda?visao=dia&amp;data={anterior}" rel="prev"><i class="bi bi-chevron-left" aria-hidden="true"></i><span class="ms-1">Dia anterior</span>' in html
    assert f'href="/agenda?visao=dia&amp;data={proximo}" rel="next"><span class="me-1">Próximo dia útil</span>' in html
    assert 'href="/agenda?visao=dia&amp;data=2026-11-05">Hoje</a>' in html


def test_dia_feriado_mostra_sem_atendimento(logado):
    html = _agenda(logado, visao="dia", data="2026-11-02")
    assert "Sem atendimento: Finados</h3>" in html
    assert "foram para a semana seguinte" in html
    assert '<p class="agenda-resumo">Nenhuma consulta.</p>' in html


def test_dia_de_fim_de_semana_mostra_o_aviso_da_tela_inicial(logado, monkeypatch):
    html = _agenda(logado, visao="dia", data="2026-11-07")
    assert '<p class="inicio-prof-sem-atendimento mb-0">Não há atendimento: fim de semana.</p>' in html
    monkeypatch.setattr(agenda_router, "agora_local", lambda: datetime(2026, 11, 7, 10, 0))
    hoje = _agenda(logado, visao="dia", data="2026-11-07")
    assert f'<p class="inicio-prof-sem-atendimento mb-0">{servico.MSG_FIM_DE_SEMANA}</p>' in hoje
    assert servico.MSG_FIM_DE_SEMANA == inicio_profissional_service.MSG_FIM_DE_SEMANA


def test_dia_vazio_leva_para_a_semana(logado):
    html = _agenda(logado, visao="dia", data="2026-11-10")
    assert "Nenhuma consulta neste dia</h3>" in html
    assert "Consultas aparecem aqui depois que o pagamento do plano é confirmado." in html
    assert '<a class="btn btn-outline-primary btn-sm" href="/agenda?visao=semana&amp;data=2026-11-10">Ver a semana</a>' in html


# --- Período ------------------------------------------------------------------------------------------

def test_periodo_formulario_dica_e_atalhos(logado):
    html = _agenda(logado, visao="periodo", inicio="2026-11-02", fim="2026-11-20")
    assert '<label for="inicio" class="form-label fw-medium">De</label>' in html
    assert '<label for="fim" class="form-label fw-medium">Até</label>' in html
    assert 'type="date" id="inicio" name="inicio"' in html and 'value="2026-11-02"' in html and 'value="2026-11-20"' in html
    assert "O período pode ter no máximo 92 dias." in html and "is-invalid" not in html
    atalhos = re.findall(r'href="/agenda\?visao=periodo&amp;inicio=([\d-]+)&amp;fim=([\d-]+)"\s*>([^<]+)</a>', html)
    assert atalhos == [("2026-11-05", "2026-11-11", "Próximos 7 dias"), ("2026-11-05", "2026-12-04", "Próximos 30 dias"),
                       ("2026-11-01", "2026-11-30", "Este mês"), ("2026-12-01", "2026-12-31", "Próximo mês")]


def test_atalhos_puros_na_virada_do_ano():
    atalhos = {a.rotulo: (a.inicio, a.fim) for a in servico.atalhos_do_periodo(date(2026, 12, 15))}
    assert atalhos["Este mês"] == (date(2026, 12, 1), date(2026, 12, 31))
    assert atalhos["Próximo mês"] == (date(2027, 1, 1), date(2027, 1, 31))
    assert atalhos["Próximos 30 dias"] == (date(2026, 12, 15), date(2027, 1, 13))
    fevereiro = {a.rotulo: (a.inicio, a.fim) for a in servico.atalhos_do_periodo(date(2028, 1, 31))}
    assert fevereiro["Próximo mês"] == (date(2028, 2, 1), date(2028, 2, 29))


def test_atalho_atual_fica_marcado(logado):
    html = _agenda(logado, visao="periodo", inicio="2026-11-01", fim="2026-11-30")
    assert re.search(r'btn-primary rounded-pill" href="[^"]+"\s*aria-current="true">Este mês<', html)


def test_periodo_resultado_resumo_semanas_e_dias_vazios(db, logado, prof, pedro):
    criar_plano(db, pedro, prof, [
        (datetime(2026, 11, 3, 9, 0), AGENDADA), (datetime(2026, 11, 5, 14, 0), AGENDADA),
        (datetime(2026, 11, 12, 14, 0), AGENDADA), (datetime(2026, 11, 13, 9, 0), AGENDADA),
    ])
    html = _agenda(logado, visao="periodo", inicio="2026-11-02", fim="2026-11-13")
    assert "02/11/2026 a 13/11/2026</h2>" in html
    assert '<p class="agenda-resumo">4 consultas, 1 em andamento, 1 aguardando registro.</p>' in html
    assert html.index("Semana de 02/11 a 06/11") < html.index("Semana de 09/11 a 13/11")
    assert re.search(r'agenda-periodo-data">3 dias</span> <span class="agenda-periodo-texto"> Sem consultas de Seg '
                     r'<span[^>]*>09/11</span> a Qua <span[^>]*>11/11</span>\. </span>', html)
    assert html.count("agenda-periodo-dia-hoje") == 1 and 'id="dia-2026-11-05"' in html
    assert re.search(r'id="dia-2026-11-02"> <div class="agenda-periodo-data"> <a[^>]*>Seg, 02/11</a> '
                     r'<span class="badge rounded-pill status-inativo">Feriado nacional</span>', html)
    assert "Sem atendimento: Finados." in html
    assert 'id="dia-2026-11-07"' not in html and 'id="dia-2026-11-08"' not in html  # só dias úteis


def test_periodo_vazio(logado):
    html = _agenda(logado, visao="periodo", inicio="2026-11-23", fim="2026-11-27")
    assert "Nenhuma consulta entre 23/11 e 27/11" in html and "Tente um período maior." in html


def test_periodo_de_92_dias_e_aceito_e_93_nao(logado):
    aceito = _agenda(logado, visao="periodo", inicio="2026-11-02", fim="2027-02-01")
    assert "02/11/2026 a 01/02/2027" in aceito and "is-invalid" not in aceito
    recusado = _agenda(logado, visao="periodo", inicio="2026-11-02", fim="2027-02-02")
    assert "O período pode ter no máximo 92 dias. Escolha uma data até 01/02/2027." in recusado
    assert servico.data_limite_do_periodo(date(2026, 11, 2)) == date(2027, 2, 1)


def _campos_invalidos(html):
    return re.findall(r'id="(inicio|fim)" name="\1" class="form-control is-invalid"', html)


@pytest.mark.parametrize("inicio, fim, mensagem, campos", [
    ("2026-11-02", "2027-03-31", "O período pode ter no máximo 92 dias. Escolha uma data até 01/02/2027.", ["fim"]),
    ("2026-11-20", "2026-11-02", "A data final não pode ser anterior à data inicial. Escolha uma data a partir de 20/11/2026.", ["fim"]),
    ("2026-11-02", "2026-02-30", "Informe uma data válida.", ["fim"]),
    ("abc", "2026-11-20", "Informe uma data válida.", ["inicio"]),
    ("20261102", "2026-11-20", "Informe uma data válida.", ["inicio"]),
    ("2026-13-01", "9999-99-99", "Informe uma data válida.", ["inicio", "fim"]),
    ("", "2026-11-20", servico.MSG_PERIODO_INCOMPLETO, ["inicio"]),
], ids=["acima_de_92", "ate_antes_de_de", "fim_inexistente", "inicio_texto", "inicio_sem_hifen", "os_dois", "sem_inicio"])
def test_periodo_invalido_marca_o_campo_e_nao_mostra_lista(db, logado, prof, pedro, inicio, fim, mensagem, campos):
    criar_plano(db, pedro, prof, [(datetime(2026, 11, 5, 9, 0), AGENDADA)])
    resposta = logado.get("/agenda", params={"visao": "periodo", "inicio": inicio, "fim": fim})
    assert resposta.status_code == 200 and resposta.headers["content-type"].startswith("text/html")
    html = _html(resposta)
    assert f'id="periodo-erro" role="alert">{mensagem}</p>' in html
    assert _campos_invalidos(html) == campos
    assert 'aria-current="page">Período</a>' in html
    assert "Escolha um período</h2>" in html
    assert "Pedro Souza" not in html and "agenda-periodo-lista" not in html and "O período pode ter no máximo 92 dias.</p>" not in html


def test_sem_parametros_continua_na_semana_atual(logado):
    html = _agenda(logado)
    assert 'aria-current="page">Semana</a>' in html and "02/11/2026 a 06/11/2026</h2>" in html


@pytest.mark.parametrize("texto, esperado", [
    ("2026-11-02", date(2026, 11, 2)),
    (" 2026-11-02 ", date(2026, 11, 2)),
    ("20261102", None),
    ("2026-W45-1", None),
    ("2026-11-2", None),
    ("２０２６-11-02", None),
    ("2026-²1-02", None),
    ("2026-02-30", None),
    ("0000-01-01", None),
    ("", None),
    (None, None),
])
def test_ler_data_iso(texto, esperado):
    assert ler_data_iso(texto) == esperado


@pytest.mark.parametrize("valor", ["²", "2026-11-0²", "9" * 400, "2026-11-02T10:00", "%00", "-1", "2026-11-02-01"])
def test_datas_estranhas_na_url_nunca_dao_500(logado, valor):
    for params in ({"visao": "dia", "data": valor}, {"visao": "semana", "data": valor},
                   {"visao": "periodo", "inicio": valor, "fim": "2026-11-20"}, {"visao": "periodo", "inicio": "2026-11-02", "fim": valor}):
        resposta = logado.get("/agenda", params=params)
        assert resposta.status_code == 200, params
        assert "Informe uma data válida." in resposta.text


# --- A registrar --------------------------------------------------------------------------------------

def test_a_registrar_linha_compacta_com_atraso(db, logado, prof, pedro):
    criar_plano(db, pedro, prof, [
        (datetime(2026, 10, 14, 9, 0), AGENDADA), (datetime(2026, 11, 3, 16, 0), AGENDADA), (datetime(2026, 11, 5, 9, 0), AGENDADA),
    ])
    html = _agenda(logado, visao="registrar")
    assert "3 consultas já terminaram e ainda não têm registro. A mais antiga aparece primeiro." in html
    itens = re.findall(r'<time class="agenda-registrar-data" datetime="[^"]+">([^<]+)</time> '
                       r'<span class="agenda-registrar-hora"><span class="font-monospace-num">([^<]+)</span></span> '
                       r'<span class="agenda-registrar-quem"><strong>([^<]+)</strong> <span class="agenda-idade">([^<]+)</span></span> '
                       r'<span class="agenda-atraso ?(agenda-atraso-destacado)?">([^<]+)</span>', html)
    assert itens == [
        ("Qua, 14/10", "09:00 – 10:00", "Pedro Souza", "7 anos", "agenda-atraso-destacado", "há 22 dias"),
        ("Ter, 03/11", "16:00 – 17:00", "Pedro Souza", "7 anos", "", "há 2 dias"),
        ("Qui, 05/11", "09:00 – 10:00", "Pedro Souza", "7 anos", "", "hoje"),
    ]
    assert html.count("Registrar atendimento<span") == 3
    assert ">Agendada<" not in html


def test_a_registrar_no_singular_e_vazia(db, logado, prof, pedro):
    assert "Nenhuma consulta esperando registro." in _agenda(logado, visao="registrar")
    criar_plano(db, pedro, prof, [(datetime(2026, 11, 4, 9, 0), AGENDADA)])
    html = _agenda(logado, visao="registrar")
    assert "1 consulta já terminou e ainda não tem registro." in html and "A mais antiga" not in html
    assert "ontem" in html


def test_a_registrar_mostra_o_ano_de_outro_ano(db, logado, prof, pedro):
    criar_plano(db, pedro, prof, [(datetime(2025, 12, 15, 9, 0), AGENDADA)])
    assert ">Seg, 15/12/2025</time>" in _agenda(logado, visao="registrar")


# --- Privacidade e consultas ao banco -----------------------------------------------------------------

def test_privacidade_em_todas_as_visoes(db, logado, prof, cliente, pedro):
    db.get(Cliente, cliente.id).cpf = "123.456.789-09"
    db.get(Cliente, cliente.id).telefone = "(54) 99999-1234"
    plano = criar_plano(db, pedro, prof, [(datetime(2026, 11, 3, 9, 0), AGENDADA), (datetime(2026, 11, 5, 14, 0), AGENDADA)])
    plano.observacao = "<b>negrito</b>"
    db.commit()
    for params in ({}, {"visao": "dia", "data": "2026-11-05"}, {"visao": "periodo", "inicio": "2026-11-02", "fim": "2026-11-06"},
                   {"visao": "registrar"}):
        html = _agenda(logado, **params)
        assert "Pedro Souza" in html and "7 anos" in html
        for proibido in ["123.456.789-09", "12345678909", "99999-1234", cliente.email, "Maria Responsável",
                         "01/03/2019", "2019-03-01", "<b>negrito</b>"]:
            assert proibido not in html, (params, proibido)


def _contar_sql(client, db, params):
    comandos = []
    ouvinte = lambda *args: comandos.append(args[2])  # noqa: E731
    event.listen(db.get_bind(), "before_cursor_execute", ouvinte)
    try:
        assert client.get("/agenda", params=params).status_code == 200
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", ouvinte)
    return len(comandos)


@pytest.mark.parametrize("params", [
    {}, {"visao": "dia", "data": "2026-11-05"}, {"visao": "periodo", "inicio": "2026-10-01", "fim": "2026-11-30"},
    {"visao": "registrar"},
], ids=["semana", "dia", "periodo", "registrar"])
def test_sem_n_mais_1(db, logado, prof, cliente, pedro, params):
    criar_plano(db, pedro, prof, [(datetime(2026, 11, 3, 9, 0), AGENDADA)])
    poucas = _contar_sql(logado, db, params)
    for i in range(6):
        paciente = criar_paciente(db, cliente.id, nome=f"Atendido {i}")
        plano = criar_plano(db, paciente, prof, [(datetime(2026, 11, 2 + i % 5, 9 + i, 0), AGENDADA),
                                                 (datetime(2026, 10, 5 + i, 9, 0), REALIZADA)])
        plano.observacao = f"Obs {i}"
    db.commit()
    _avaliar(db, datetime(2026, 10, 5, 9, 0), False)
    assert _contar_sql(logado, db, params) == poucas


def test_periodo_com_erro_nao_consulta_as_sessoes(db, logado, prof, pedro):
    _contar_sql(logado, db, {})  # aquece: a 1ª requisição recarrega o usuário logado
    valido = _contar_sql(logado, db, {"visao": "periodo", "inicio": "2026-11-02", "fim": "2026-11-06"})
    invalido = _contar_sql(logado, db, {"visao": "periodo", "inicio": "2026-11-02", "fim": "2027-11-06"})
    assert invalido == valido - 1


def test_pendente_de_pagamento_continua_fora(db, logado, prof, pedro):
    criar_plano(db, pedro, prof, [(datetime(2026, 11, 6, 9, 0), PENDENTE)])
    for params in ({}, {"visao": "dia", "data": "2026-11-06"}, {"visao": "periodo", "inicio": "2026-11-02", "fim": "2026-11-06"}):
        assert "Pedro Souza" not in _agenda(logado, **params)


def test_tela_continua_sem_javascript_proprio(db, logado, prof, pedro):
    criar_plano(db, pedro, prof, [(datetime(2026, 11, 5, 9, 0), AGENDADA)])
    for params in ({}, {"visao": "dia"}, {"visao": "periodo", "inicio": "2026-11-02", "fim": "2026-11-06"}, {"visao": "registrar"}):
        html = _agenda(logado, **params)
        assert "<script>" not in re.sub(r'<script src="[^"]+"></script>', "", html)
        assert "onclick" not in html.lower() and "agenda.js" not in html
