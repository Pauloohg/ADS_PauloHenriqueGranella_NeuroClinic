from datetime import date, datetime, time

import pytest

from app.services.plano_service import DiasSemanaInvalidos, HorarioInvalido, simular_sessoes, validar_horario

SEXTA = 4


def test_rn06_feriado_de_20_11_desloca_a_sessao_para_a_semana_seguinte_sem_mudar_a_quantidade():
    # Mensal às sextas de 06/11 a 05/12/2026: 06, 13, 20 (Consciência Negra), 27/11 e 04/12 = 5 sessões
    simulacao = simular_sessoes([SEXTA], time(14), 1, "mensal", datetime(2026, 11, 2, 10, 0))

    assert simulacao.feriados_pulados == [date(2026, 11, 20)]
    assert simulacao.quantidade == len(simulacao.sessoes) == 5
    assert [s.date() for s in simulacao.sessoes] == [
        date(2026, 11, 6), date(2026, 11, 13), date(2026, 11, 27), date(2026, 12, 4), date(2026, 12, 11),
    ]


@pytest.mark.parametrize("hora", [8, 9, 10, 11, 14, 15, 16, 17])
def test_rn08_horarios_validos_em_hora_cheia(hora):
    assert validar_horario(time(hora)) == time(hora)


@pytest.mark.parametrize("horario", [time(7), time(12), time(13), time(18), time(9, 30)], ids=str)
def test_rn08_horarios_invalidos_sao_recusados(horario):
    with pytest.raises(HorarioInvalido):
        validar_horario(horario)


def test_rn08_sabado_e_recusado():
    with pytest.raises(DiasSemanaInvalidos):
        simular_sessoes([5], time(14), 1, "mensal", datetime(2026, 8, 3, 9, 0))
