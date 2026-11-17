from datetime import datetime

import pytest

from app.models.sessao import StatusSessao
from tests.fabricas import criar_plano, logar


@pytest.fixture
def plano_da_maria(db, carla, pedro):
    return criar_plano(db, pedro, carla, [(datetime(2026, 8, 10, 14, 0), StatusSessao.AGENDADA)])


def test_seguranca_cliente_b_recebe_404_nos_dados_do_cliente_a(client, joana, pedro, plano_da_maria):
    logar(client, joana.email)

    assert client.get(f"/planos/{plano_da_maria.id}").status_code == 404
    assert client.get(f"/historico?atendido={pedro.id}").status_code == 404
    assert client.get(f"/pacientes/{pedro.id}/editar").status_code == 404


def test_seguranca_cliente_em_rota_do_admin_recebe_403(client, maria):
    logar(client, maria.email)

    assert client.get("/admin/painel").status_code == 403
    assert client.get("/admin/clientes").status_code == 403


def test_seguranca_visitante_vai_ao_login(client, plano_da_maria):
    for caminho in (f"/planos/{plano_da_maria.id}", "/historico", "/admin/painel", "/agenda"):
        resposta = client.get(caminho, follow_redirects=False)
        assert (resposta.status_code, resposta.headers["location"]) == (303, "/login"), caminho


def test_seguranca_caractere_nul_recebe_400(client):
    resposta = client.post("/login", data={"email": "a\x00b@exemplo.com", "senha": "x"}, headers={"Accept": "*/*"})

    assert resposta.status_code == 400
    assert resposta.json() == {"detail": "Os dados enviados contêm caracteres inválidos."}
