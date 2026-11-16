# B4 da exploração de 14/11: horário com fuso vindo do usuário é recusado com 400 (antes quebrava a comparação: 500)
from datetime import datetime

import pytest
from sqlalchemy import select

from app.models.sessao import Sessao, StatusSessao
from app.routers import cliente as cliente_router
from app.routers import consultas as consultas_router
from app.services import remarcacao_service
from app.services.remarcacao_service import RemarcacaoInvalida
from tests.fabricas import criar_cliente, criar_especialidade, criar_funcionario, criar_paciente, criar_plano, logar

AGORA = datetime(2026, 11, 3, 10, 0)  # terça-feira, horário local
AGORA_UTC = datetime(2026, 11, 3, 13, 0)
QUI = datetime(2026, 11, 5, 14, 0)
COM_FUSO = ["2026-11-10T15:00:00+05:00", "2026-11-10T15:00:00Z", "2026-11-10T15:00:00-03:00", "2026-11-10T15:00+00:00"]


@pytest.fixture(autouse=True)
def relogio_fixo(monkeypatch):
    monkeypatch.setattr(consultas_router, "agora_local", lambda: AGORA)
    monkeypatch.setattr(consultas_router, "agora_utc", lambda: AGORA_UTC)
    monkeypatch.setattr(cliente_router, "agora_local", lambda: AGORA)


@pytest.fixture
def fono(db):
    return criar_funcionario(db, criar_especialidade(db, "Fonoaudiologia"))


@pytest.fixture
def sessao(db, client, fono):
    cliente = criar_cliente(db)
    plano = criar_plano(db, criar_paciente(db, cliente.id), fono, [(QUI, StatusSessao.AGENDADA)],
                        criado_em=datetime(2026, 10, 1, 12, 0))
    logar(client, cliente.email)
    return db.scalar(select(Sessao.id).where(Sessao.plano_id == plano.id))


@pytest.mark.parametrize("texto", COM_FUSO)
def test_interpretar_horario_recusa_fuso(texto):
    with pytest.raises(RemarcacaoInvalida) as exc:
        remarcacao_service.interpretar_horario(texto)
    assert exc.value.mensagem == remarcacao_service.MSG_ESCOLHA_HORARIO


@pytest.mark.parametrize("texto, esperado", [
    ("2026-11-10T15:00", datetime(2026, 11, 10, 15, 0)), (" 2026-11-10T15:00 ", datetime(2026, 11, 10, 15, 0)),
])
def test_interpretar_horario_aceita_o_formato_da_tela(texto, esperado):
    assert remarcacao_service.interpretar_horario(texto) == esperado


@pytest.mark.parametrize("texto", COM_FUSO)
def test_post_remarcar_com_fuso_da_400_e_nada_muda(client, db, sessao, texto):
    resposta = client.post(f"/consultas/{sessao}/remarcar", data={"novo_horario": texto}, follow_redirects=False)

    assert resposta.status_code == 400
    assert remarcacao_service.MSG_ESCOLHA_HORARIO in resposta.text


def test_post_remarcar_com_o_valor_da_tela_continua_funcionando(client, db, sessao):
    resposta = client.post(f"/consultas/{sessao}/remarcar", data={"novo_horario": "2026-11-10T15:00"}, follow_redirects=False)

    assert resposta.status_code == 303 and resposta.headers["location"].startswith("/consultas?remarcada=")


@pytest.mark.parametrize("horario", ["14:00+05:00", "14:00Z", "14:00:00-03:00"])
def test_simulacao_do_rf05_recusa_horario_com_fuso(client, db, fono, horario):
    logar(client, criar_cliente(db, email="outra@exemplo.com").email)

    resposta = client.get(f"/contratar/{fono.usuario.id}/simulacao",
                          params={"dias": "0", "horario": horario, "frequencia": "1", "duracao": "mensal"})

    assert resposta.status_code == 400 and "erro" in resposta.json()


def test_simulacao_com_horario_da_tela_continua_funcionando(client, db, fono):
    logar(client, criar_cliente(db, email="outra@exemplo.com").email)

    resposta = client.get(f"/contratar/{fono.usuario.id}/simulacao",
                          params={"dias": "0", "horario": "14:00", "frequencia": "1", "duracao": "mensal"})

    assert resposta.status_code == 200 and resposta.json()["quantidade"] > 0
