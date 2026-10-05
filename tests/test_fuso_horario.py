from datetime import datetime, timezone

import pytest

from app.routers.auth import utc_para_local


def test_utc_para_local_subtrai_3_horas_e_remove_o_fuso():
    local = utc_para_local(datetime(2026, 10, 17, 10, 0, tzinfo=timezone.utc))
    assert local == datetime(2026, 10, 17, 7, 0)
    assert local.tzinfo is None


@pytest.mark.parametrize("hora, minuto", [(0, 0), (1, 30), (2, 59)])
def test_utc_para_local_vira_para_o_dia_anterior_na_madrugada_utc(hora, minuto):
    local = utc_para_local(datetime(2026, 10, 17, hora, minuto, tzinfo=timezone.utc))
    assert local == datetime(2026, 10, 16, hora + 21, minuto)


def test_utc_para_local_as_3h_utc_ainda_e_o_mesmo_dia():
    assert utc_para_local(datetime(2026, 10, 17, 3, 0, tzinfo=timezone.utc)) == datetime(2026, 10, 17, 0, 0)


def test_utc_para_local_sem_horario_de_verao_em_janeiro():
    assert utc_para_local(datetime(2027, 1, 15, 12, 0, tzinfo=timezone.utc)) == datetime(2027, 1, 15, 9, 0)
