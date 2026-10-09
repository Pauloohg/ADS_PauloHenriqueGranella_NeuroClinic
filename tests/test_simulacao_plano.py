from datetime import date, datetime, time
from decimal import Decimal

import pytest
from sqlalchemy import event

from app.models.plano import Plano
from app.models.sessao import Sessao, StatusSessao
from app.services import funcionario_service, plano_service
from app.services.plano_service import (
    DiasSemanaInvalidos,
    DuracaoInvalida,
    FrequenciaInvalida,
    HorarioInvalido,
    ProfissionalIndisponivel,
    calcular_valor_total,
    simular_plano,
    simular_sessoes,
    verificar_conflitos,
)
from tests.fabricas import criar_especialidade, criar_funcionario, criar_plano_com_sessao

SEG, TER, QUA, QUI, SEX, SAB, DOM = range(7)
H14 = time(14, 0)
AGORA_CONFLITOS = datetime(2026, 8, 1, 10, 0)  # local
CRIADO_EM_UTC = datetime(2026, 8, 1, 12, 0)  # 09:00 local: pendentes ficam dentro do prazo até 13:00 local


def _datas(simulacao):
    return [s.date() for s in simulacao.sessoes]


# --- simular_sessoes: quantidade pela janela real ------------------------------------------------

def test_mensal_em_mes_de_31_dias_seg_qua():
    # Janela [seg 03/08, qui 03/09): segundas 3,10,17,24,31/08 e quartas 5,12,19,26/08 e 02/09
    sim = simular_sessoes([SEG, QUA], H14, 2, "mensal", datetime(2026, 8, 1, 10, 0))
    assert sim.quantidade == 10
    assert sim.data_inicio == date(2026, 8, 3) and sim.data_fim == date(2026, 9, 2)
    assert sim.feriados_pulados == []


def test_mensal_em_mes_de_30_dias_seg_qua():
    # Janela [seg 01/06, qua 01/07): segundas 1,8,15,22,29/06 e quartas 3,10,17,24/06
    sim = simular_sessoes([SEG, QUA], H14, 2, "mensal", datetime(2026, 5, 30, 10, 0))
    assert sim.quantidade == 9
    assert sim.data_inicio == date(2026, 6, 1) and sim.data_fim == date(2026, 6, 29)


def test_quantidade_nao_e_frequencia_vezes_quatro():
    junho = simular_sessoes([SEG, QUA], H14, 2, "mensal", datetime(2026, 5, 30, 10, 0))
    agosto = simular_sessoes([SEG, QUA], H14, 2, "mensal", datetime(2026, 8, 1, 10, 0))
    assert 2 * 4 not in (junho.quantidade, agosto.quantidade)
    assert junho.quantidade != agosto.quantidade


@pytest.mark.parametrize(
    "duracao, quantidade, data_fim",
    [
        ("quinzenal", 2, date(2026, 8, 10)),   # [03/08, 17/08)
        ("mensal", 5, date(2026, 8, 31)),      # [03/08, 03/09)
        ("bimestral", 9, date(2026, 10, 5)),   # [03/08, 03/10): 9 segundas; 07/09 pulada, estende até 05/10
        # [03/08/2026, 03/08/2027): 53 segundas; 07/09, 12/10 e 02/11/2026 puladas, estende até 23/08/2027
        ("anual", 53, date(2027, 8, 23)),
    ],
)
def test_duracoes_semanais_as_segundas(duracao, quantidade, data_fim):
    sim = simular_sessoes([SEG], H14, 1, duracao, datetime(2026, 8, 1, 10, 0))
    assert sim.quantidade == quantidade == len(sim.sessoes)
    assert sim.data_fim == data_fim


def test_duas_vezes_por_semana_quinzenal():
    sim = simular_sessoes([QUI, TER], H14, 2, "quinzenal", datetime(2026, 8, 1, 10, 0))
    assert _datas(sim) == [date(2026, 8, d) for d in (4, 6, 11, 13)]


def test_tres_vezes_por_semana_quinzenal():
    sim = simular_sessoes([SEX, SEG, QUA], H14, 3, "quinzenal", datetime(2026, 8, 1, 10, 0))
    assert _datas(sim) == [date(2026, 8, d) for d in (3, 5, 7, 10, 12, 14)]
    assert all(s.time() == H14 for s in sim.sessoes)


# --- Primeiro slot -------------------------------------------------------------------------------

def test_rn10_slot_3h59_depois_de_agora_e_pulado():
    sim = simular_sessoes([SEG], H14, 1, "quinzenal", datetime(2026, 8, 3, 10, 1))  # seg 03/08, 14:00 - 3h59
    assert sim.data_inicio == date(2026, 8, 10)


def test_rn10_slot_exatamente_4h_depois_e_aceito():
    sim = simular_sessoes([SEG], H14, 1, "quinzenal", datetime(2026, 8, 3, 10, 0))
    assert sim.sessoes[0] == datetime(2026, 8, 3, 14, 0)


def test_rn10_virada_de_dia_vai_para_o_proximo_dia_elegivel():
    # 15:19 + 4h = 19:19: a sessão das 17h de segunda 03/08 não cabe; a próxima elegível é terça 04/08
    seg_ter = simular_sessoes([SEG, TER], time(17), 2, "quinzenal", datetime(2026, 8, 3, 15, 19))
    so_seg = simular_sessoes([SEG], time(17), 1, "quinzenal", datetime(2026, 8, 3, 15, 19))
    assert seg_ter.sessoes[0] == datetime(2026, 8, 4, 17, 0)
    assert so_seg.sessoes[0] == datetime(2026, 8, 10, 17, 0)


def test_rn10_limite_passa_da_meia_noite():
    # seg 03/08 21:00 + 4h = ter 04/08 01:00: a sessão das 08h de terça já é elegível
    sim = simular_sessoes([TER], time(8), 1, "quinzenal", datetime(2026, 8, 3, 21, 0))
    assert sim.sessoes[0] == datetime(2026, 8, 4, 8, 0)


# --- RN06: feriados -------------------------------------------------------------------------------

def test_feriado_no_meio_do_plano_e_pulado_e_o_plano_estende():
    # Janela [31/08, 30/09): 5 segundas; 07/09 (Independência) é pulada e o plano vai até 05/10
    sim = simular_sessoes([SEG], H14, 1, "mensal", datetime(2026, 8, 28, 10, 0))
    assert sim.quantidade == 5
    assert _datas(sim) == [date(2026, 8, 31), date(2026, 9, 14), date(2026, 9, 21), date(2026, 9, 28), date(2026, 10, 5)]
    assert sim.feriados_pulados == [date(2026, 9, 7)]
    assert sim.data_fim == date(2026, 10, 5)
    assert len(set(sim.sessoes)) == len(sim.sessoes)


def test_feriado_no_meio_com_dois_dias_por_semana_nao_duplica_slot():
    # Seg+qua, quinzenal [31/08, 14/09): 31/08, 02/09, 07/09 (feriado), 09/09 -> estende com 14/09
    sim = simular_sessoes([SEG, QUA], H14, 2, "quinzenal", datetime(2026, 8, 28, 10, 0))
    assert _datas(sim) == [date(2026, 8, 31), date(2026, 9, 2), date(2026, 9, 9), date(2026, 9, 14)]
    assert sim.feriados_pulados == [date(2026, 9, 7)]
    assert len(set(sim.sessoes)) == len(sim.sessoes)


def test_primeiro_slot_cai_em_feriado():
    # agora = qui 08/10; janela [12/10, 12/11): segundas 12/10 (Aparecida), 19, 26/10, 02/11 (Finados), 09/11
    sim = simular_sessoes([SEG], H14, 1, "mensal", datetime(2026, 10, 8, 10, 0))
    assert sim.quantidade == 5
    assert _datas(sim) == [date(2026, 10, 19), date(2026, 10, 26), date(2026, 11, 9), date(2026, 11, 16), date(2026, 11, 23)]
    assert sim.feriados_pulados == [date(2026, 10, 12), date(2026, 11, 2)]
    assert sim.data_inicio == date(2026, 10, 19)


def test_feriados_consecutivos_na_virada_do_ano():
    # Sextas; janela [18/12/2026, 18/01/2027): 18/12, 25/12 (Natal), 01/01 (Confraternização), 08/01, 15/01
    sim = simular_sessoes([SEX], H14, 1, "mensal", datetime(2026, 12, 14, 10, 0))
    assert sim.quantidade == 5
    assert _datas(sim) == [date(2026, 12, 18), date(2027, 1, 8), date(2027, 1, 15), date(2027, 1, 22), date(2027, 1, 29)]
    assert sim.feriados_pulados == [date(2026, 12, 25), date(2027, 1, 1)]


def test_feriados_opcionais_da_biblioteca_nao_sao_pulados():
    # Carnaval (16 e 17/02/2026) e Corpus Christi (04/06/2026) não estão nos feriados padrão do holidays
    carnaval = simular_sessoes([SEG, TER], H14, 2, "quinzenal", datetime(2026, 2, 13, 10, 0))
    corpus = simular_sessoes([QUI], H14, 1, "quinzenal", datetime(2026, 6, 1, 10, 0))
    assert date(2026, 2, 16) in _datas(carnaval) and date(2026, 2, 17) in _datas(carnaval)
    assert date(2026, 6, 4) in _datas(corpus)
    assert carnaval.feriados_pulados == corpus.feriados_pulados == []


# --- Validações de entrada -----------------------------------------------------------------------

@pytest.mark.parametrize("dias", [[SAB], [DOM], [SEX, SAB]])
def test_fim_de_semana_rejeitado(dias):
    with pytest.raises(DiasSemanaInvalidos, match="segunda a sexta"):
        simular_sessoes(dias, H14, len(dias), "mensal", datetime(2026, 8, 1, 10, 0))


@pytest.mark.parametrize(
    "horario",
    [
        time(7, 0), time(18, 0), time(23, 0),  # fora do expediente
        time(12, 0), time(13, 0),  # almoço
        time(10, 30), time(17, 30), time(7, 59), time(17, 1), time(14, 0, 30),  # não é hora cheia
    ],
)
def test_horario_invalido_rejeitado(horario):
    with pytest.raises(HorarioInvalido, match="hora cheia: 08:00, 09:00, 10:00, 11:00, 14:00, 15:00, 16:00 ou 17:00"):
        simular_sessoes([SEG], horario, 1, "mensal", datetime(2026, 8, 1, 10, 0))


@pytest.mark.parametrize("hora", [8, 9, 10, 11, 14, 15, 16, 17])
def test_horarios_de_atendimento_aceitos(hora):
    assert simular_sessoes([SEG], time(hora), 1, "quinzenal", datetime(2026, 8, 1, 7, 0)).quantidade == 2


@pytest.mark.parametrize("frequencia", [0, 4, 5, -1, True])
def test_frequencia_fora_de_1_a_3_rejeitada(frequencia):
    dias = [SEG, TER, QUA, QUI, SEX][: max(int(frequencia), 0)] or [SEG]
    with pytest.raises(FrequenciaInvalida, match="1, 2 ou 3"):
        simular_sessoes(dias, H14, frequencia, "mensal", datetime(2026, 8, 1, 10, 0))


def test_frequencia_diferente_da_quantidade_de_dias_rejeitada():
    with pytest.raises(DiasSemanaInvalidos):
        simular_sessoes([SEG, QUA], H14, 3, "mensal", datetime(2026, 8, 1, 10, 0))


@pytest.mark.parametrize("duracao", ["semestral", "", "Mensal", "2 meses", "trimestral"])
def test_duracao_invalida_rejeitada(duracao):
    with pytest.raises(DuracaoInvalida, match="quinzenal, mensal, bimestral ou anual"):
        simular_sessoes([SEG], H14, 1, duracao, datetime(2026, 8, 1, 10, 0))


# --- Valor -----------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "valor, quantidade, total",
    [("150.00", 5, "750.00"), ("99.90", 3, "299.70"), ("180.00", 53, "9540.00"), ("0.10", 3, "0.30")],
)
def test_calcular_valor_total_em_decimal(valor, quantidade, total):
    resultado = calcular_valor_total(Decimal(valor), quantidade)
    assert isinstance(resultado, Decimal)
    assert resultado == Decimal(total) and str(resultado) == total


# --- RN01: conflitos ------------------------------------------------------------------------------

@pytest.fixture
def fono(db):
    return criar_especialidade(db, "Fonoaudiologia", "150.00")


@pytest.fixture
def carla(db, fono):
    return criar_funcionario(db, fono, email="carla@exemplo.com", nome="Carla Fono")


def _ocupar(db, funcionario, especialidade, data_hora, status=StatusSessao.AGENDADA, foi_remarcada=False):
    plano = db.query(Plano).filter_by(funcionario_id=funcionario.usuario.id).first()
    if plano is None:
        plano = criar_plano_com_sessao(
            db, funcionario.usuario.id, especialidade, status, data_hora=data_hora, criado_em=CRIADO_EM_UTC
        )
        db.query(Sessao).filter_by(plano_id=plano.id).one().foi_remarcada = foi_remarcada
    else:
        db.add(Sessao(plano_id=plano.id, data_hora=data_hora, status=status, foi_remarcada=foi_remarcada))
    db.commit()


@pytest.mark.parametrize("status", [StatusSessao.AGENDADA, StatusSessao.PENDENTE_PAGAMENTO])
def test_conflito_parcial_com_sessao_existente_fora_da_hora_cheia(db, fono, carla, status):
    # Sessão inserida direto no banco às 10:30 (10:30–11:30): cruza as propostas das 10:00 e das 11:00
    _ocupar(db, carla, fono, datetime(2026, 8, 3, 10, 30), status)
    propostas = [datetime(2026, 8, 3, h, 0) for h in (9, 10, 11, 14)] + [datetime(2026, 8, 10, 10, 0)]
    conflitos = verificar_conflitos(db, carla.usuario.id, propostas, AGORA_CONFLITOS)
    assert conflitos == [datetime(2026, 8, 3, 10, 0), datetime(2026, 8, 3, 11, 0)]


def test_conflito_com_sessao_que_comeca_antes_da_menor_data(db, fono, carla):
    _ocupar(db, carla, fono, datetime(2026, 8, 3, 9, 30))
    assert verificar_conflitos(db, carla.usuario.id, [datetime(2026, 8, 3, 10, 0)], AGORA_CONFLITOS) == [datetime(2026, 8, 3, 10, 0)]


def test_mesmo_horario_conflita(db, fono, carla):
    _ocupar(db, carla, fono, datetime(2026, 8, 3, 10, 0))
    assert verificar_conflitos(db, carla.usuario.id, [datetime(2026, 8, 3, 10, 0)], AGORA_CONFLITOS) == [datetime(2026, 8, 3, 10, 0)]


def test_sessoes_adjacentes_nao_conflitam(db, fono, carla):
    _ocupar(db, carla, fono, datetime(2026, 8, 3, 10, 0))
    propostas = [datetime(2026, 8, 3, 9, 0), datetime(2026, 8, 3, 11, 0)]
    assert verificar_conflitos(db, carla.usuario.id, propostas, AGORA_CONFLITOS) == []


def test_sessao_de_outro_funcionario_nao_conflita(db, fono, carla):
    colega = criar_funcionario(db, fono, email="colega@exemplo.com")
    _ocupar(db, colega, fono, datetime(2026, 8, 3, 10, 0))
    assert verificar_conflitos(db, carla.usuario.id, [datetime(2026, 8, 3, 10, 0)], AGORA_CONFLITOS) == []


def test_sessao_realizada_e_sessao_passada_nao_conflitam(db, fono, carla):
    _ocupar(db, carla, fono, datetime(2026, 8, 3, 10, 0), StatusSessao.REALIZADA)
    _ocupar(db, carla, fono, datetime(2026, 7, 27, 10, 0), StatusSessao.AGENDADA)
    propostas = [datetime(2026, 8, 3, 10, 0), datetime(2026, 8, 10, 10, 0)]
    assert verificar_conflitos(db, carla.usuario.id, propostas, AGORA_CONFLITOS) == []


def test_sessao_remarcada_conflita_pelo_horario_atual(db, fono, carla):
    _ocupar(db, carla, fono, datetime(2026, 8, 5, 15, 0), foi_remarcada=True)
    propostas = [datetime(2026, 8, 3, 15, 0), datetime(2026, 8, 5, 15, 0)]
    assert verificar_conflitos(db, carla.usuario.id, propostas, AGORA_CONFLITOS) == [datetime(2026, 8, 5, 15, 0)]


def test_verificar_conflitos_faz_uma_unica_consulta(db, fono, carla):
    for dia in (3, 10, 17):
        _ocupar(db, carla, fono, datetime(2026, 8, dia, 14, 0))
    propostas = simular_sessoes([SEG, QUA, SEX], H14, 3, "anual", datetime(2026, 8, 1, 10, 0)).sessoes
    funcionario_id = carla.usuario.id  # lido antes de contar: o objeto expira no commit e recarregaria aqui dentro

    consultas = []
    motor = db.get_bind()
    contar = lambda *args, **kwargs: consultas.append(args[2])  # noqa: E731
    event.listen(motor, "before_cursor_execute", contar)
    try:
        conflitos = verificar_conflitos(db, funcionario_id, propostas, AGORA_CONFLITOS)
    finally:
        event.remove(motor, "before_cursor_execute", contar)

    assert len(propostas) > 100
    assert len(consultas) == 1
    assert conflitos == [datetime(2026, 8, d, 14, 0) for d in (3, 10, 17)]


def test_verificar_conflitos_sem_datas_nao_consulta(db, carla):
    assert verificar_conflitos(db, carla.usuario.id, [], AGORA_CONFLITOS) == []


# --- simular_plano (orquestrador) -----------------------------------------------------------------

def test_simular_plano_usa_valor_atual_da_especialidade(db, fono, carla):
    fono.valor_sessao = Decimal("160.00")
    db.commit()
    sim = simular_plano(db, carla.usuario.id, [SEG], H14, 1, "mensal", datetime(2026, 8, 1, 10, 0))
    assert (sim.quantidade, sim.valor_sessao, sim.valor_total) == (5, Decimal("160.00"), Decimal("800.00"))
    assert sim.especialidade_id == fono.id and sim.conflitos == []


def test_simular_plano_com_feriado_e_conflito(db, fono, carla):
    _ocupar(db, carla, fono, datetime(2026, 9, 14, 14, 30))
    sim = simular_plano(db, carla.usuario.id, [SEG], H14, 1, "mensal", datetime(2026, 8, 28, 10, 0))
    assert sim.quantidade == 5 and sim.valor_total == Decimal("750.00")
    assert (sim.data_inicio, sim.data_fim) == (date(2026, 8, 31), date(2026, 10, 5))
    assert sim.feriados_pulados == [date(2026, 9, 7)]
    assert sim.conflitos == [datetime(2026, 9, 14, 14, 0)]


def test_simular_plano_nao_persiste_nada(db, fono, carla):
    antes = db.query(Sessao).count()
    simular_plano(db, carla.usuario.id, [SEG, QUA], H14, 2, "bimestral", datetime(2026, 8, 1, 10, 0))
    assert db.query(Sessao).count() == antes
    assert not db.new and not db.dirty


def test_simular_plano_com_funcionario_inativo_rejeitado(db, carla):
    funcionario_service.desativar(db, carla.usuario.id, datetime(2026, 8, 1, 10, 0))
    with pytest.raises(ProfissionalIndisponivel):
        simular_plano(db, carla.usuario.id, [SEG], H14, 1, "mensal", datetime(2026, 8, 1, 10, 0))


def test_simular_plano_com_funcionario_inexistente_rejeitado(db):
    with pytest.raises(ProfissionalIndisponivel):
        simular_plano(db, 999, [SEG], H14, 1, "mensal", datetime(2026, 8, 1, 10, 0))


def test_erros_de_dominio_sao_plano_invalido():
    for erro in (DiasSemanaInvalidos, HorarioInvalido, FrequenciaInvalida, DuracaoInvalida, ProfissionalIndisponivel):
        assert issubclass(erro, plano_service.PlanoInvalido)
