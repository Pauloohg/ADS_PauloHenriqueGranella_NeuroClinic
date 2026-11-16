# B5 da exploração de 14/11: data.transparent.id que não é texto válido vira "id desconhecido" (200), nunca 500
import json
import logging
from datetime import datetime, timezone

import pytest

from app.models.pagamento import FormaPagamento, Pagamento, StatusPagamento
from app.models.sessao import StatusSessao
from app.routers import webhooks as webhooks_router
from app.services.pagamento_service import calcular_assinatura
from tests.abacatepay_falsa import SECRET_WEBHOOK_TESTE
from tests.fabricas import criar_cliente, criar_especialidade, criar_funcionario, criar_paciente, criar_plano

URL = f"/webhooks/abacatepay?webhookSecret={SECRET_WEBHOOK_TESTE}"
COBRANCA_ID = "pix_char_b5teste"
GIGANTE = "pix_" + "x" * 5000
INVALIDOS = [123, ["pix_char_b5teste"], {"id": "pix_char_b5teste"}, True, "pix_char\x00b5", GIGANTE]
NOMES = ["numero", "lista", "objeto", "booleano", "texto-com-nul", "texto-gigante"]


@pytest.fixture(autouse=True)
def relogio_fixo(monkeypatch, api_falsa):
    monkeypatch.setattr(webhooks_router, "agora_local", lambda: datetime(2026, 8, 1, 10, 0))
    monkeypatch.setattr(webhooks_router, "agora_utc", lambda: datetime(2026, 8, 1, 13, 0, tzinfo=timezone.utc))


@pytest.fixture
def pagamento(db):
    funcionario = criar_funcionario(db, criar_especialidade(db))
    paciente = criar_paciente(db, criar_cliente(db).id)
    plano = criar_plano(db, paciente, funcionario, [(datetime(2026, 8, 10, 14, 0), StatusSessao.PENDENTE_PAGAMENTO)],
                        criado_em=datetime(2026, 8, 1, 12, 0))
    pag = Pagamento(plano_id=plano.id, forma_pagamento=FormaPagamento.PIX, status=StatusPagamento.PENDENTE,
                    abacatepay_payment_id=COBRANCA_ID)
    db.add(pag)
    db.commit()
    return pag


def _enviar(client, transparente: dict):
    corpo = json.dumps({"event": "transparent.completed", "data": {"transparent": transparente}}).encode()
    return client.post(URL, content=corpo, headers={"X-Webhook-Signature": calcular_assinatura(corpo),
                                                    "Content-Type": "application/json"})


@pytest.mark.parametrize("valor", INVALIDOS, ids=NOMES)
def test_id_que_nao_e_texto_valido_responde_como_id_ausente(client, db, pagamento, valor):
    ausente = _enviar(client, {"amount": 15000})

    resposta = _enviar(client, {"id": valor, "amount": 15000})

    assert resposta.status_code == ausente.status_code == 200
    assert resposta.json() == ausente.json() == {"mensagem": "desconhecido"}
    db.expire_all()
    assert db.get(Pagamento, pagamento.id).status == StatusPagamento.PENDENTE


@pytest.mark.parametrize("valor", INVALIDOS, ids=NOMES)
def test_log_registra_so_o_tipo_do_valor(client, pagamento, caplog, valor):
    with caplog.at_level(logging.INFO):
        _enviar(client, {"id": valor, "amount": 15000})

    registro = "\n".join(r.getMessage() for r in caplog.records)
    assert type(valor).__name__ in registro
    assert "x" * 60 not in registro and "\x00" not in registro and "pix_char_b5teste" not in registro


def test_id_valido_continua_confirmando_uma_vez(client, db, pagamento):
    primeira = _enviar(client, {"id": COBRANCA_ID, "amount": 15000})
    segunda = _enviar(client, {"id": COBRANCA_ID, "amount": 15000})

    assert primeira.json() == {"mensagem": "confirmado"} and segunda.json() == {"mensagem": "ja_confirmado"}
