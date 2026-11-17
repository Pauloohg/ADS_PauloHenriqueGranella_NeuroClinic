from datetime import datetime

import pytest

from app.models.funcionario import Funcionario
from app.models.sessao import StatusSessao
from app.services import funcionario_service, plano_service
from tests.fabricas import criar_plano

AGORA = datetime(2026, 8, 3, 9, 0)
CRIADO_EM_UTC = datetime(2026, 8, 3, 12, 0)  # 09:00 local: Pix até 13:00, carência de remoção até 14:00
OCUPADO = datetime(2026, 8, 10, 14, 0)  # 14:00–15:00


@pytest.fixture
def sessao_das_14h(db, carla, pedro):
    return criar_plano(db, pedro, carla, [(OCUPADO, StatusSessao.AGENDADA)])


@pytest.mark.parametrize("inicio", [datetime(2026, 8, 10, 13, 1), datetime(2026, 8, 10, 14, 1)], ids=["13h01", "14h01"])
def test_rn01_sobreposicao_de_59_minutos_e_conflito(db, carla, sessao_das_14h, inicio):
    assert plano_service.verificar_conflitos(db, carla.usuario.id, [inicio], AGORA) == [inicio]


@pytest.mark.parametrize("inicio", [datetime(2026, 8, 10, 13, 0), datetime(2026, 8, 10, 15, 0)], ids=["13h00", "15h00"])
def test_rn01_encostar_no_inicio_ou_no_fim_nao_e_conflito(db, carla, sessao_das_14h, inicio):
    assert plano_service.verificar_conflitos(db, carla.usuario.id, [inicio], AGORA) == []


def test_rn05_desativar_profissional_com_sessao_futura_agendada_e_bloqueado(db, carla, sessao_das_14h):
    with pytest.raises(funcionario_service.FuncionarioComSessoesFuturas):
        funcionario_service.desativar(db, carla.usuario.id, AGORA)

    assert db.get(Funcionario, carla.usuario.id).ativo is True


def _reserva_sem_pagamento(db, carla, pedro):
    return criar_plano(db, pedro, carla, [(OCUPADO, StatusSessao.PENDENTE_PAGAMENTO)], criado_em=CRIADO_EM_UTC)


def test_rn05_reserva_vencida_ainda_na_carencia_continua_bloqueando(db, carla, pedro):
    _reserva_sem_pagamento(db, carla, pedro)
    agora = datetime(2026, 8, 3, 13, 30)  # Pix venceu às 13:00, mas a carência vai até 14:00

    plano_service.remover_planos_vencidos(db, agora)
    with pytest.raises(funcionario_service.FuncionarioComSessoesFuturas):
        funcionario_service.desativar(db, carla.usuario.id, agora)


def test_rn05_reserva_vencida_fora_da_carencia_libera_a_desativacao(db, carla, pedro):
    _reserva_sem_pagamento(db, carla, pedro)
    agora = datetime(2026, 8, 3, 14, 1)

    assert plano_service.remover_planos_vencidos(db, agora) == 1
    funcionario_service.desativar(db, carla.usuario.id, agora)

    assert db.get(Funcionario, carla.usuario.id).ativo is False
