from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.core.config import CARENCIA_REMOCAO_MINUTOS, PRAZO_PAGAMENTO_HORAS
from app.core.fuso import local_para_utc
from app.models.cliente import Cliente
from app.models.pagamento import Pagamento, StatusPagamento
from app.models.plano import Plano, StatusPlano
from app.models.sessao import Sessao, StatusSessao
from app.routers import cliente as cliente_router
from app.routers import inicio as inicio_router
from app.services import plano_service
from tests.fabricas import (
    criar_cliente,
    criar_especialidade,
    criar_funcionario,
    criar_pagamento,
    criar_paciente,
    criar_plano,
    logar,
)

AGENDADA = StatusSessao.AGENDADA
PENDENTE = StatusSessao.PENDENTE_PAGAMENTO
REALIZADA = StatusSessao.REALIZADA

AGORA = datetime(2026, 8, 3, 20, 0)  # segunda, horário local
LIMITE_LOCAL = AGORA - timedelta(hours=PRAZO_PAGAMENTO_HORAS, minutes=CARENCIA_REMOCAO_MINUTOS)  # criado às 15:00 local
SESSAO = datetime(2026, 8, 10, 14, 0)


@pytest.fixture
def fono(db):
    return criar_funcionario(db, criar_especialidade(db, "Fonoaudiologia", "150.00"))


@pytest.fixture
def cliente(db):
    return criar_cliente(db)


@pytest.fixture
def paciente(db, cliente):
    return criar_paciente(db, cliente.id)


def _criado_em(local: datetime) -> datetime:
    return local_para_utc(local)


def _planos(db) -> set[int]:
    return set(db.scalars(select(Plano.id)))


# --- Plano encerrado (derivado) ------------------------------------------------------------------

@pytest.mark.parametrize("data_fim, pendente, esperado", [
    (date(2026, 8, 2), False, True),  # ontem
    (date(2026, 8, 3), False, False),  # hoje: a última sessão ainda é hoje
    (date(2026, 8, 4), False, False),
    (date(2026, 8, 2), True, False),  # pendente nunca é encerrado
])
def test_plano_encerrado_pela_data_fim_e_sem_pendente(data_fim, pendente, esperado):
    assert plano_service.plano_encerrado(data_fim, pendente, AGORA) is esperado


def test_virada_do_dia_usa_a_data_local(db, fono, paciente):
    plano = criar_plano(db, paciente, fono, [(datetime(2026, 8, 3, 9, 0), REALIZADA)])
    assert not plano_service.plano_esta_encerrado(db, plano, datetime(2026, 8, 3, 23, 59))
    assert plano_service.plano_esta_encerrado(db, plano, datetime(2026, 8, 4, 0, 0))


def test_plano_esta_encerrado_consulta_as_sessoes(db, fono, paciente):
    pago = criar_plano(db, paciente, fono, [(datetime(2026, 7, 27, 9, 0), REALIZADA)])
    pendente = criar_plano(db, paciente, fono, [(datetime(2026, 7, 28, 9, 0), PENDENTE)], criado_em=_criado_em(LIMITE_LOCAL))
    assert plano_service.plano_esta_encerrado(db, pago, AGORA)
    assert not plano_service.plano_esta_encerrado(db, pendente, AGORA)


def test_coluna_status_nao_decide_se_o_plano_esta_encerrado(db, fono, paciente):
    marcado_ativo = criar_plano(db, paciente, fono, [(datetime(2026, 7, 27, 9, 0), REALIZADA)], status_plano=StatusPlano.ATIVO)
    marcado_encerrado = criar_plano(db, paciente, fono, [(SESSAO, AGENDADA)], status_plano=StatusPlano.ENCERRADO)

    assert plano_service.plano_esta_encerrado(db, marcado_ativo, AGORA)
    assert not plano_service.plano_esta_encerrado(db, marcado_encerrado, AGORA)
    ativos = db.scalars(select(Plano.id).where(plano_service.condicao_plano_ativo(AGORA))).all()
    assert ativos == [marcado_encerrado.id]


def test_condicao_plano_ativo_exclui_encerrado_e_aguardando(db, fono, paciente):
    hoje = criar_plano(db, paciente, fono, [(datetime(2026, 8, 3, 9, 0), REALIZADA)])
    criar_plano(db, paciente, fono, [(datetime(2026, 8, 2, 9, 0), REALIZADA)])  # ontem
    criar_plano(db, paciente, fono, [(SESSAO, PENDENTE)], criado_em=_criado_em(AGORA))  # aguardando
    futuro = criar_plano(db, paciente, fono, [(datetime(2026, 7, 27, 9, 0), REALIZADA), (SESSAO, AGENDADA)])

    ativos = db.scalars(select(Plano.id).where(plano_service.condicao_plano_ativo(AGORA)).order_by(Plano.id)).all()
    assert ativos == [hoje.id, futuro.id]


# --- Limpeza dos planos com pagamento vencido (RN09) ---------------------------------------------

def test_apaga_somente_o_vencido_depois_da_carencia(db, fono, paciente):
    vencido_id = criar_plano(db, paciente, fono, [(SESSAO, PENDENTE), (SESSAO + timedelta(days=7), PENDENTE)],
                             criado_em=_criado_em(LIMITE_LOCAL - timedelta(seconds=1))).id
    no_limite = criar_plano(db, paciente, fono, [(SESSAO, PENDENTE)], criado_em=_criado_em(LIMITE_LOCAL))

    assert plano_service.remover_planos_vencidos(db, AGORA) == 1

    assert _planos(db) == {no_limite.id}
    assert db.scalar(select(func.count()).select_from(Sessao).where(Sessao.plano_id == vencido_id)) == 0
    assert db.scalar(select(func.count()).select_from(Sessao)) == 1


def test_dentro_da_carencia_o_horario_ja_esta_livre_mas_o_plano_fica(db, fono, paciente):
    criado = LIMITE_LOCAL + timedelta(minutes=30)  # prazo venceu há 30 minutos
    plano = criar_plano(db, paciente, fono, [(SESSAO, PENDENTE)], criado_em=_criado_em(criado))

    assert plano_service.remover_planos_vencidos(db, AGORA) == 0
    assert _planos(db) == {plano.id}
    assert not plano_service.ocupa_horario(PENDENTE, plano.criado_em, AGORA)  # RN09 continua liberando aos 4h
    assert plano_service.verificar_conflitos(db, fono.usuario.id, [SESSAO], AGORA) == []


@pytest.mark.parametrize("status", [AGENDADA, REALIZADA])
def test_preserva_plano_com_sessao_agendada_ou_realizada(db, fono, paciente, status):
    antigo = _criado_em(datetime(2026, 7, 1, 9, 0))
    so_pago = criar_plano(db, paciente, fono, [(SESSAO, status)], criado_em=antigo)
    misto = criar_plano(db, paciente, fono, [(SESSAO, status), (SESSAO + timedelta(days=7), PENDENTE)], criado_em=antigo)

    assert plano_service.remover_planos_vencidos(db, AGORA) == 0
    assert _planos(db) == {so_pago.id, misto.id}


def test_preserva_plano_com_pagamento_confirmado(db, fono, paciente):
    plano = criar_plano(db, paciente, fono, [(SESSAO, PENDENTE)], criado_em=_criado_em(datetime(2026, 7, 1, 9, 0)))
    criar_pagamento(db, plano, confirmado_em=datetime(2026, 7, 1, 13, 0))

    assert plano_service.remover_planos_vencidos(db, AGORA) == 0
    assert _planos(db) == {plano.id}


def test_apaga_junto_o_pagamento_pendente(db, fono, paciente):
    plano = criar_plano(db, paciente, fono, [(SESSAO, PENDENTE)], criado_em=_criado_em(datetime(2026, 7, 1, 9, 0)))
    criar_pagamento(db, plano, status=StatusPagamento.PENDENTE)

    assert plano_service.remover_planos_vencidos(db, AGORA) == 1
    assert _planos(db) == set()
    assert db.scalar(select(func.count()).select_from(Pagamento)) == 0


def test_converte_criado_em_de_utc_para_local(db, fono, paciente):
    # 18:30 UTC = 15:30 local: sem a conversão, o prazo + carência (15:30 + 5h) pareceria vencido às 20:00
    criar_plano(db, paciente, fono, [(SESSAO, PENDENTE)], criado_em=datetime(2026, 8, 3, 18, 30))
    assert plano_service.remover_planos_vencidos(db, AGORA) == 0
    assert plano_service.remover_planos_vencidos(db, datetime(2026, 8, 3, 20, 31)) == 1
    assert _planos(db) == set()


# --- Pontos que chamam a limpeza -----------------------------------------------------------------

def test_inicio_do_cliente_remove_os_vencidos(client, db, monkeypatch, fono, cliente, paciente):
    monkeypatch.setattr(inicio_router, "agora_local", lambda: AGORA)
    criar_plano(db, paciente, fono, [(SESSAO, PENDENTE)], criado_em=_criado_em(datetime(2026, 8, 3, 9, 0)))
    no_prazo_id = criar_plano(db, paciente, fono, [(SESSAO + timedelta(hours=1), PENDENTE)], criado_em=_criado_em(AGORA)).id
    logar(client, cliente.email)

    assert client.get("/").status_code == 200
    assert _planos(db) == {no_prazo_id}


def test_confirmacao_remove_os_vencidos_e_ocupa_o_horario(client, db, monkeypatch, fono, cliente, paciente):
    monkeypatch.setattr(cliente_router, "agora_local", lambda: AGORA)
    monkeypatch.setattr(cliente_router, "agora_utc", lambda: local_para_utc(AGORA).replace(tzinfo=timezone.utc))
    db.get(Cliente, cliente.id).cpf = "529.982.247-25"
    db.commit()
    outra = criar_cliente(db, email="outra@exemplo.com", nome="Outra Cliente")
    alheio = criar_paciente(db, outra.id, nome="Alheio")
    criar_plano(db, alheio, fono, [(SESSAO, PENDENTE)], criado_em=_criado_em(datetime(2026, 8, 3, 9, 0)))
    logar(client, cliente.email)

    form = {"paciente_id": str(paciente.id), "celula": ["0-14"], "frequencia_semanal": "1",
            "duracao": "quinzenal", "observacao": ""}
    resposta = client.post(f"/contratar/{fono.usuario.id}/confirmar", data=form, follow_redirects=False)

    assert resposta.status_code == 303
    assert db.scalars(select(Plano.paciente_id)).all() == [paciente.id]  # o vencido de outra cliente foi apagado
    assert db.scalar(select(func.min(Sessao.data_hora))) == SESSAO
