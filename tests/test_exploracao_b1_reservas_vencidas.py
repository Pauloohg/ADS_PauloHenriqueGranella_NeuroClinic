# B1 da exploração de 14/11: reserva vencida fora da carência não pode prender a RN05/RN07 nem as contagens do Admin
from datetime import datetime, timedelta

import pytest

from app.core.fuso import local_para_utc
from app.models.funcionario import Funcionario
from app.models.pagamento import FormaPagamento, Pagamento, StatusPagamento
from app.models.plano import Plano
from app.models.sessao import StatusSessao
from app.routers import admin as admin_router
from app.services import plano_service
from tests.fabricas import criar_admin, criar_cliente, criar_especialidade, criar_funcionario, criar_paciente, criar_plano, logar

PENDENTE, AGENDADA, REALIZADA = StatusSessao.PENDENTE_PAGAMENTO, StatusSessao.AGENDADA, StatusSessao.REALIZADA
CRIADO_LOCAL = datetime(2026, 11, 16, 9, 0)  # segunda-feira
CRIADO_UTC = local_para_utc(CRIADO_LOCAL)
SESSAO = datetime(2026, 11, 27, 16, 0)  # sexta-feira, futura em todos os cenários
PRAZO_PIX = plano_service.prazo_pagamento(CRIADO_UTC, SESSAO.date(), FormaPagamento.PIX)
LIMITE_PIX = plano_service.limite_remocao(CRIADO_UTC, SESSAO.date(), FormaPagamento.PIX)
PRAZO_BOLETO = plano_service.prazo_pagamento(CRIADO_UTC, SESSAO.date(), FormaPagamento.BOLETO)
LIMITE_BOLETO = plano_service.limite_remocao(CRIADO_UTC, SESSAO.date(), FormaPagamento.BOLETO)
UM_MINUTO = timedelta(minutes=1)


@pytest.fixture
def fono(db):
    return criar_especialidade(db, "Fonoaudiologia")


@pytest.fixture
def psico(db):
    return criar_especialidade(db, "Psicologia")


@pytest.fixture
def carla(db, fono):
    return criar_funcionario(db, fono, email="carla@exemplo.com", nome="Carla Fono")


@pytest.fixture
def paciente(db):
    return criar_paciente(db, criar_cliente(db).id)


@pytest.fixture
def admin(client, db):
    logar(client, criar_admin(db).email)
    return client


def _relogio(monkeypatch, agora):
    monkeypatch.setattr(admin_router, "agora_local", lambda: agora)


def _reserva(db, paciente, carla, status=PENDENTE, forma=None, pagamento=None):
    plano = criar_plano(db, paciente, carla, [(SESSAO, status)], criado_em=CRIADO_UTC)
    if forma is not None:
        db.add(Pagamento(plano_id=plano.id, forma_pagamento=forma, status=pagamento or StatusPagamento.PENDENTE))
        db.commit()
    return plano.id  # o id é guardado antes: depois da remoção o objeto fica inválido


def _desativar(admin, carla):
    return admin.post(f"/admin/funcionarios/{carla.usuario.id}/desativar", follow_redirects=False)


def _trocar(admin, carla, psico):
    return admin.post(
        f"/admin/funcionarios/{carla.usuario.id}/editar", data={"nome": "Carla Fono", "especialidade_id": str(psico.id)},
        follow_redirects=False,
    )


def _ativo(db, carla) -> bool:
    db.expire_all()
    return db.get(Funcionario, carla.usuario.id).ativo


def _existe(db, plano_id) -> bool:
    db.expire_all()
    return db.get(Plano, plano_id) is not None


# --- Pix sem cobrança e Pix com cobrança pendente -----------------------------------------------------

@pytest.mark.parametrize("forma", [None, FormaPagamento.PIX], ids=["sem-cobranca", "pix-pendente"])
def test_reserva_pix_vencida_fora_da_carencia_nao_impede_desativar(db, admin, carla, paciente, monkeypatch, forma):
    plano_id = _reserva(db, paciente, carla, forma=forma)
    _relogio(monkeypatch, LIMITE_PIX + UM_MINUTO)

    resposta = _desativar(admin, carla)

    assert resposta.status_code == 303 and resposta.headers["location"].endswith("ok=desativado")
    assert not _ativo(db, carla) and not _existe(db, plano_id)


def test_confirmacao_de_desativacao_abre_sem_erro_de_consultas_futuras(db, admin, carla, paciente, monkeypatch):
    _reserva(db, paciente, carla)
    _relogio(monkeypatch, LIMITE_PIX + UM_MINUTO)

    resposta = admin.get(f"/admin/funcionarios/{carla.usuario.id}/desativar", follow_redirects=False)

    assert resposta.status_code == 200


def test_reserva_pix_vencida_fora_da_carencia_nao_impede_trocar_especialidade(db, admin, carla, psico, paciente, monkeypatch):
    _reserva(db, paciente, carla)
    _relogio(monkeypatch, LIMITE_PIX + UM_MINUTO)

    resposta = _trocar(admin, carla, psico)

    assert resposta.status_code == 303
    db.expire_all()
    assert db.get(Funcionario, carla.usuario.id).especialidade_id == psico.id


def test_lista_e_edicao_do_admin_nao_contam_reserva_vencida(db, admin, carla, paciente, monkeypatch):
    plano_id = _reserva(db, paciente, carla)
    _relogio(monkeypatch, LIMITE_PIX + UM_MINUTO)

    lista = admin.get("/admin/funcionarios")
    edicao = admin.get(f"/admin/funcionarios/{carla.usuario.id}/editar")

    assert lista.status_code == edicao.status_code == 200
    assert not _existe(db, plano_id)
    assert '<span class="font-monospace-num">0</span> consultas futuras' in lista.text
    assert "prof-trava" not in edicao.text


# --- Dentro da carência continua bloqueando (RN05 literal) ------------------------------------------

def test_reserva_pix_vencida_dentro_da_carencia_continua_bloqueando(db, admin, carla, psico, paciente, monkeypatch):
    plano_id = _reserva(db, paciente, carla)
    _relogio(monkeypatch, PRAZO_PIX + UM_MINUTO)
    assert PRAZO_PIX + UM_MINUTO < LIMITE_PIX

    assert _desativar(admin, carla).status_code == 409
    assert _trocar(admin, carla, psico).status_code == 409
    assert _ativo(db, carla) and _existe(db, plano_id)


def test_reserva_pix_no_limite_exato_da_carencia_ainda_bloqueia(db, admin, carla, paciente, monkeypatch):
    _reserva(db, paciente, carla)
    _relogio(monkeypatch, LIMITE_PIX)

    assert _desativar(admin, carla).status_code == 409


# --- Pago, agendado ou realizado nunca é removido ----------------------------------------------------

@pytest.mark.parametrize("status, pagamento", [
    (PENDENTE, StatusPagamento.CONFIRMADO), (AGENDADA, StatusPagamento.CONFIRMADO), (AGENDADA, None),
], ids=["pendente-com-pagamento-confirmado", "agendada-paga", "agendada-sem-pagamento"])
def test_plano_pago_ou_agendado_nunca_e_removido_e_continua_bloqueando(db, admin, carla, paciente, monkeypatch, status, pagamento):
    plano_id = _reserva(db, paciente, carla, status=status, forma=FormaPagamento.PIX if pagamento else None, pagamento=pagamento)
    _relogio(monkeypatch, LIMITE_PIX + timedelta(days=3))

    assert _desativar(admin, carla).status_code == 409
    assert _existe(db, plano_id)


def test_plano_com_sessao_realizada_nunca_e_removido(db, admin, carla, paciente, monkeypatch):
    plano_id = criar_plano(db, paciente, carla, [(datetime(2026, 11, 17, 9, 0), REALIZADA), (SESSAO, PENDENTE)], criado_em=CRIADO_UTC).id
    _relogio(monkeypatch, LIMITE_PIX + timedelta(days=3))

    assert _desativar(admin, carla).status_code == 409
    assert _existe(db, plano_id)


# --- Boleto: vencimento e carência de 1 dia útil ------------------------------------------------------

def test_boleto_vencido_dentro_da_carencia_de_um_dia_util_bloqueia(db, admin, carla, paciente, monkeypatch):
    plano_id = _reserva(db, paciente, carla, forma=FormaPagamento.BOLETO)
    _relogio(monkeypatch, PRAZO_BOLETO + UM_MINUTO)
    assert PRAZO_BOLETO + UM_MINUTO < LIMITE_BOLETO

    assert _desativar(admin, carla).status_code == 409
    assert _existe(db, plano_id)


def test_boleto_no_prazo_bloqueia(db, admin, carla, paciente, monkeypatch):
    _reserva(db, paciente, carla, forma=FormaPagamento.BOLETO)
    _relogio(monkeypatch, LIMITE_PIX + UM_MINUTO)  # o prazo do Pix já passou, o do boleto não
    assert LIMITE_PIX + UM_MINUTO < PRAZO_BOLETO

    assert _desativar(admin, carla).status_code == 409


def test_boleto_depois_da_carencia_libera_a_desativacao(db, admin, carla, paciente, monkeypatch):
    plano_id = _reserva(db, paciente, carla, forma=FormaPagamento.BOLETO)
    _relogio(monkeypatch, LIMITE_BOLETO + timedelta(seconds=1))

    assert _desativar(admin, carla).status_code == 303
    assert not _existe(db, plano_id)
