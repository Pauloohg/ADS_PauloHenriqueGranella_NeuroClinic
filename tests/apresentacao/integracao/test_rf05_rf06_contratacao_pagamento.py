from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models.cliente import Cliente
from app.models.pagamento import StatusPagamento
from app.models.plano import Plano
from app.models.sessao import Sessao, StatusSessao
from app.services import contratacao_service, pagamento_service
from tests.fabricas import criar_paciente, criar_pagamento, criar_plano

AGORA = datetime(2026, 8, 3, 9, 0)  # segunda, horário local
CRIADO_EM_UTC = datetime(2026, 8, 3, 12, 0)  # o mesmo instante em UTC
SEGUNDAS_14H = [datetime(2026, 8, dia, 14, 0) for dia in (3, 10, 17, 24, 31)]


def _contratar_segundas_14h(db, maria, carla, pedro):
    return contratacao_service.confirmar_plano(
        db, db.get(Cliente, maria.id), carla.usuario.id, pedro, ["0-14"], "1", "mensal", "", AGORA, CRIADO_EM_UTC,
    )


def _contar(db, modelo) -> int:
    return db.scalar(select(func.count()).select_from(modelo))


def test_rf05_contratacao_gera_plano_e_sessoes_pendente_pagamento(db, maria, carla, pedro):
    plano, criado = _contratar_segundas_14h(db, maria, carla, pedro)

    sessoes = db.execute(select(Sessao.data_hora, Sessao.status).where(Sessao.plano_id == plano.id).order_by(Sessao.data_hora)).all()
    assert criado is True
    assert [data for data, _ in sessoes] == SEGUNDAS_14H
    assert {status for _, status in sessoes} == {StatusSessao.PENDENTE_PAGAMENTO}
    # RN11: 5 x R$ 150,00 = R$ 750,00; mensal 5% = R$ 37,50
    assert (plano.valor_desconto, plano.valor_total) == (Decimal("37.50"), Decimal("712.50"))


def test_rf05_conflito_na_confirmacao_nao_grava_plano_nem_sessao(db, maria, joana, carla, pedro):
    criar_plano(db, criar_paciente(db, joana.id, nome="Lia Lima"), carla, [(SEGUNDAS_14H[2], StatusSessao.AGENDADA)])

    with pytest.raises(contratacao_service.ContratacaoInvalida):
        _contratar_segundas_14h(db, maria, carla, pedro)

    assert (_contar(db, Plano), _contar(db, Sessao)) == (1, 1)  # só o plano que já existia


def test_rf06_confirmar_pagamento_e_idempotente(db, carla, pedro):
    plano = criar_plano(db, pedro, carla, [(d, StatusSessao.PENDENTE_PAGAMENTO) for d in SEGUNDAS_14H], criado_em=CRIADO_EM_UTC)
    pagamento = criar_pagamento(db, plano, status=StatusPagamento.PENDENTE)

    primeiro = pagamento_service.confirmar_pagamento(db, plano, pagamento, AGORA, CRIADO_EM_UTC)
    segundo = pagamento_service.confirmar_pagamento(db, plano, pagamento, AGORA, datetime(2026, 8, 3, 12, 5))

    assert (primeiro.situacao, segundo.situacao) == ("confirmado", "ja_confirmado")
    assert pagamento.confirmado_em == CRIADO_EM_UTC  # a segunda entrega não sobrescreve
    assert set(db.scalars(select(Sessao.status).where(Sessao.plano_id == plano.id))) == {StatusSessao.AGENDADA}
