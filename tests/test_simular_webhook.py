import json
from contextlib import contextmanager
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models.cliente import Cliente
from app.models.pagamento import FormaPagamento, Pagamento, StatusPagamento
from app.models.sessao import Sessao, StatusSessao
from app.routers import cliente as cliente_router
from app.routers import inicio as inicio_router
from app.routers import webhooks as webhooks_router
from app.services import contratacao_service, pagamento_service
from scripts import simular_webhook
from tests.abacatepay_falsa import CHAVE_API_TESTE, SECRET_WEBHOOK_TESTE, cobranca_boleto
from tests.fabricas import criar_cliente, criar_especialidade, criar_funcionario, criar_paciente, criar_plano, logar

AGORA = datetime(2026, 8, 3, 10, 0)  # segunda, horário local
AGORA_UTC = datetime(2026, 8, 3, 13, 0, tzinfo=timezone.utc)
CRIADO_EM_UTC = datetime(2026, 8, 3, 12, 0)  # 09:00 local
SEGUNDAS = [datetime(2026, 8, dia, 14, 0) for dia in (17, 24, 31)]
CHAVE_DEV = "abc_dev_chave_de_teste_nao_deve_aparecer"
URL = simular_webhook.URL_WEBHOOK_LOCAL


@pytest.fixture(autouse=True)
def ambiente(monkeypatch, api_falsa):
    for modulo in (cliente_router, inicio_router, webhooks_router):
        monkeypatch.setattr(modulo, "agora_local", lambda: AGORA)
    for modulo in (cliente_router, webhooks_router):
        monkeypatch.setattr(modulo, "agora_utc", lambda: AGORA_UTC)
    monkeypatch.setattr(settings, "ABACATEPAY_API_KEY", CHAVE_DEV)
    monkeypatch.setattr(pagamento_service, "enviar_email", _sem_email)


async def _sem_email(*args):
    return True


def _sessao_de_teste(db):
    @contextmanager
    def abrir():
        yield db
    return abrir


@pytest.fixture
def maria(db):
    usuario = criar_cliente(db, email="maria@exemplo.com", nome="Maria Souza")
    db.get(Cliente, usuario.id).cpf = "529.982.247-25"
    db.commit()
    return usuario


@pytest.fixture
def plano(db, maria):
    carla = criar_funcionario(db, criar_especialidade(db, "Fonoaudiologia", "150.00"), email="carla@exemplo.com")
    pedro = criar_paciente(db, maria.id, nome="Pedro Souza")
    return criar_plano(
        db, pedro, carla, [(data, StatusSessao.PENDENTE_PAGAMENTO) for data in SEGUNDAS], criado_em=CRIADO_EM_UTC,
        valor_total="450.00",
    )


@pytest.fixture
def boleto_gerado(client, db, plano, maria, api_falsa):
    # Boleto criado pelo fluxo real da tela, com a AbacatePay simulada pela api_falsa
    api_falsa.responder("/transparents/create", cobranca_boleto(valor=45000))
    logar(client, maria.email)
    assert client.post(f"/planos/{plano.id}/pagamento", data={"forma": "boleto"}, follow_redirects=False).status_code == 303
    pagamento = db.scalar(select(Pagamento))
    assert (pagamento.forma_pagamento, pagamento.status) == (FormaPagamento.BOLETO, StatusPagamento.PENDENTE)
    return pagamento


def _status_sessoes(db, plano) -> set:
    db.expire_all()
    return set(db.scalars(select(Sessao.status).where(Sessao.plano_id == plano.id)))


def _envio(db, pagamento, plano):
    evento = simular_webhook.montar_evento(pagamento, plano, AGORA_UTC)
    return simular_webhook.montar_envio(evento, SECRET_WEBHOOK_TESTE)


# --- (a) Corpo e assinatura aceitos pela rota ------------------------------------------------

def test_script_confirma_o_boleto_pela_rota_do_webhook(client, db, plano, boleto_gerado, maria, api_falsa, capsys):
    criacoes = len(api_falsa.requisicoes)

    assert simular_webhook.main([str(plano.id)], _sessao_de_teste(db), client) == 0

    saida = capsys.readouterr().out
    assert saida.strip() == "HTTP 200: webhook aceito; pagamento confirmado e sessões agendadas."
    assert len(api_falsa.requisicoes) == criacoes  # o script nunca chama a AbacatePay
    db.expire_all()
    assert db.get(Pagamento, boleto_gerado.id).status == StatusPagamento.CONFIRMADO
    assert _status_sessoes(db, plano) == {StatusSessao.AGENDADA}
    detalhe = contratacao_service.detalhar_plano(db, maria.id, plano.id, AGORA)
    assert detalhe.situacao == "ativo"


def test_corpo_segue_o_formato_documentado_e_le_o_id_da_cobranca(db, plano, boleto_gerado):
    params, corpo, cabecalhos = _envio(db, boleto_gerado, plano)
    evento = json.loads(corpo)

    assert params == {"webhookSecret": SECRET_WEBHOOK_TESTE}
    assert evento["event"] == "transparent.completed" and evento["devMode"] is True
    assert evento["data"]["transparent"]["id"] == boleto_gerado.abacatepay_payment_id
    assert evento["data"]["transparent"]["amount"] == 45000 and evento["data"]["transparent"]["status"] == "PAID"
    assert evento["data"]["payerInformation"]["method"] == "BOLETO" and "BOLETO" in evento["data"]["payerInformation"]
    assert cabecalhos["X-Webhook-Signature"] == pagamento_service.calcular_assinatura(corpo)
    assert pagamento_service.assinatura_valida(corpo, cabecalhos["X-Webhook-Signature"])
    for sensivel in ("529.982.247-25", "52998224725", "maria@exemplo.com", "Maria Souza"):
        assert sensivel not in corpo.decode(), sensivel


# --- (b) Assinatura adulterada e segredo errado ----------------------------------------------

def test_assinatura_adulterada_e_recusada(client, db, plano, boleto_gerado):
    params, corpo, cabecalhos = _envio(db, boleto_gerado, plano)
    adulterada = {**cabecalhos, "X-Webhook-Signature": "A" + cabecalhos["X-Webhook-Signature"][1:]}

    resposta = client.post(URL, params=params, content=corpo, headers=adulterada)
    assert resposta.status_code == 401

    corpo_alterado = corpo.replace(b'"amount": 45000', b'"amount": 1')
    assert corpo_alterado != corpo
    assert client.post(URL, params=params, content=corpo_alterado, headers=cabecalhos).status_code == 401

    db.expire_all()
    assert db.get(Pagamento, boleto_gerado.id).status == StatusPagamento.PENDENTE
    assert _status_sessoes(db, plano) == {StatusSessao.PENDENTE_PAGAMENTO}


def test_segredo_errado_e_recusado(client, db, plano, boleto_gerado):
    _, corpo, cabecalhos = _envio(db, boleto_gerado, plano)

    assert client.post(URL, params={"webhookSecret": "errado"}, content=corpo, headers=cabecalhos).status_code == 401
    assert client.post(URL, content=corpo, headers=cabecalhos).status_code == 401

    db.expire_all()
    assert db.get(Pagamento, boleto_gerado.id).status == StatusPagamento.PENDENTE


# --- Proteções do script ---------------------------------------------------------------------

class _ClienteQueNaoDeveSerUsado:
    def post(self, *args, **kwargs):
        raise AssertionError("o script não deveria enviar nada")


def test_recusa_chave_que_nao_e_de_dev_mode(monkeypatch, db, plano, boleto_gerado, capsys):
    monkeypatch.setattr(settings, "ABACATEPAY_API_KEY", CHAVE_API_TESTE)
    assert simular_webhook.main([str(plano.id)], _sessao_de_teste(db), _ClienteQueNaoDeveSerUsado()) == 1
    assert "não é de Dev mode" in capsys.readouterr().out


@pytest.mark.parametrize("url", [
    "https://neuroclinic.ngrok-free.app/webhooks/abacatepay", "http://192.168.0.10:8000/webhooks/abacatepay",
    "http://localhost.exemplo.com/webhooks/abacatepay",
])
def test_recusa_destino_que_nao_e_localhost(db, plano, boleto_gerado, capsys, url):
    assert simular_webhook.main([str(plano.id)], _sessao_de_teste(db), _ClienteQueNaoDeveSerUsado(), url) == 1
    assert "só pode ser enviado para localhost" in capsys.readouterr().out


def test_pagamento_ja_confirmado_nao_reenvia(db, plano, boleto_gerado, capsys):
    boleto_gerado.status = StatusPagamento.CONFIRMADO
    db.commit()
    assert simular_webhook.main([str(plano.id)], _sessao_de_teste(db), _ClienteQueNaoDeveSerUsado()) == 0
    assert "já está confirmado" in capsys.readouterr().out


def test_servidor_parado_mostra_frase_clara(db, plano, boleto_gerado, capsys):
    class ServidorParado:
        def post(self, *args, **kwargs):
            raise httpx.ConnectError("conexão recusada")

    assert simular_webhook.main([str(plano.id)], _sessao_de_teste(db), ServidorParado()) == 1
    assert "O servidor não está rodando em localhost:8000" in capsys.readouterr().out


def test_plano_sem_cobranca_e_uso_invalido(db, plano, capsys):
    assert simular_webhook.main([str(plano.id)], _sessao_de_teste(db), _ClienteQueNaoDeveSerUsado()) == 1
    assert "não tem cobrança" in capsys.readouterr().out
    for argumentos in ([], ["abc"], ["1", "2"]):
        assert simular_webhook.main(argumentos, _sessao_de_teste(db), _ClienteQueNaoDeveSerUsado()) == 1
        assert "Uso:" in capsys.readouterr().out


def test_saida_nao_expoe_chave_segredo_nem_corpo(client, db, plano, boleto_gerado, capsys):
    simular_webhook.main([str(plano.id)], _sessao_de_teste(db), client)
    saida = capsys.readouterr().out
    for sensivel in (CHAVE_DEV, SECRET_WEBHOOK_TESTE, boleto_gerado.abacatepay_payment_id, "transparent", "{"):
        assert sensivel not in saida, sensivel
    assert len(saida.strip().splitlines()) == 1
