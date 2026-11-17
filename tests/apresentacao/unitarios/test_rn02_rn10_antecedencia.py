from datetime import datetime, time

from app.models.sessao import StatusSessao
from app.services.plano_service import simular_sessoes
from app.services.remarcacao_service import MOTIVO_MENOS_DE_4H, motivo_bloqueio

SESSAO = datetime(2026, 8, 10, 14, 0)
SEGUNDA = 0


def test_rn02_remarcar_com_exatamente_4_horas_de_antecedencia_pode():
    assert motivo_bloqueio(StatusSessao.AGENDADA, SESSAO, datetime(2026, 8, 10, 10, 0)) is None


def test_rn02_remarcar_com_3h59_de_antecedencia_nao_pode():
    assert motivo_bloqueio(StatusSessao.AGENDADA, SESSAO, datetime(2026, 8, 10, 10, 1)) == MOTIVO_MENOS_DE_4H


def test_rn10_primeira_sessao_exatamente_4_horas_depois_e_aceita():
    simulacao = simular_sessoes([SEGUNDA], time(14), 1, "quinzenal", datetime(2026, 8, 3, 10, 0))

    assert simulacao.sessoes[0] == datetime(2026, 8, 3, 14, 0)


def test_rn10_primeira_sessao_com_menos_de_4_horas_vai_para_a_semana_seguinte():
    simulacao = simular_sessoes([SEGUNDA], time(14), 1, "quinzenal", datetime(2026, 8, 3, 10, 1))

    assert simulacao.sessoes[0] == datetime(2026, 8, 10, 14, 0)
