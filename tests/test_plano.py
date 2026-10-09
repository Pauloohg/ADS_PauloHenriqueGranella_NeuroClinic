from datetime import date, time
from decimal import Decimal

import pytest

from app.models.plano import Plano
from app.services.plano_service import DiasSemanaInvalidos, validar_dias_semana
from tests.fabricas import criar_cliente, criar_especialidade, criar_funcionario, criar_paciente


def test_plano_grava_e_le_dias_semana_como_lista(db):
    especialidade = criar_especialidade(db)
    funcionario = criar_funcionario(db, especialidade)
    paciente = criar_paciente(db, criar_cliente(db).id)
    db.add(Plano(
        paciente_id=paciente.id,
        funcionario_id=funcionario.usuario.id,
        especialidade_id=especialidade.id,
        dias_semana=[1, 3],
        horario=time(14, 0),
        frequencia_semanal=2,
        duracao="2 meses",
        valor_total=Decimal("2400.00"),
        data_inicio=date(2026, 3, 17),
        data_fim=date(2026, 5, 12),
    ))
    db.commit()
    db.expire_all()

    assert db.query(Plano).one().dias_semana == [1, 3]


@pytest.mark.parametrize("dias, frequencia", [([0], 1), ([3, 1], 2), ([0, 2, 4], 3)])
def test_validar_dias_semana_aceita_quantidade_igual_a_frequencia(dias, frequencia):
    assert validar_dias_semana(dias, frequencia) == sorted(dias)


@pytest.mark.parametrize(
    "dias, frequencia",
    [([1], 2), ([1, 3], 1), ([], 1), ([1, 1], 2), ([7], 1), ([-1], 1), ([True], 1)],
)
def test_validar_dias_semana_recusa_lista_incompativel(dias, frequencia):
    with pytest.raises(DiasSemanaInvalidos):
        validar_dias_semana(dias, frequencia)
