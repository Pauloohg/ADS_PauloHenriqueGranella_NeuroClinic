import json
from decimal import Decimal

from sqlalchemy import select

from app.models.paciente import Paciente
from app.models.pagamento import Pagamento, StatusPagamento
from app.models.plano import Plano
from app.models.sessao import Sessao, StatusSessao
from app.services.pagamento_service import calcular_assinatura
from tests.abacatepay_falsa import SECRET_WEBHOOK_TESTE, cobranca_pix
from tests.fabricas import logar

EMAIL = "lucia@exemplo.com"
SENHA_LUCIA = "senha-da-lucia"
COBRANCA = cobranca_pix(valor=71250, id_="pix_char_apresentacao")


def _webhook_pagamento_confirmado(client, cobranca_id: str):
    corpo = json.dumps({"event": "transparent.completed", "data": {"transparent": {"id": cobranca_id, "amount": 71250}}}).encode()
    return client.post(
        f"/webhooks/abacatepay?webhookSecret={SECRET_WEBHOOK_TESTE}", content=corpo,
        headers={"Content-Type": "application/json", "X-Webhook-Signature": calcular_assinatura(corpo)},
    )


def test_rf01_rf06_cliente_cadastra_contrata_paga_e_fica_com_plano_ativo(client, db, carla, api_falsa, emails):
    # 1. Cadastro real (hash de produção) e login
    resposta = client.post("/cadastro", data={"nome": "Lúcia Lima", "email": EMAIL, "telefone": "(54) 99999-1234", "senha": SENHA_LUCIA}, follow_redirects=False)
    assert (resposta.status_code, resposta.headers["location"]) == (303, "/login?cadastro=ok")
    logar(client, EMAIL, SENHA_LUCIA)
    perfil = {"nome": "Lúcia Lima", "email": EMAIL, "telefone": "(54) 99999-1234", "cpf": "529.982.247-25"}
    assert client.post("/perfil", data=perfil, follow_redirects=False).status_code == 303

    # 2. Atendido
    resposta = client.post("/pacientes/novo", data={"nome": "Davi Lima", "data_nascimento": "2018-05-03"}, follow_redirects=False)
    assert resposta.status_code == 303
    davi = db.scalar(select(Paciente))

    # 3. Contratação: segundas às 14h, mensal (o relógio está em seg 03/08 09:00)
    dados = {"paciente_id": str(davi.id), "celula": ["0-14"], "frequencia_semanal": "1", "duracao": "mensal", "observacao": ""}
    resposta = client.post(f"/contratar/{carla.usuario.id}/confirmar", data=dados, follow_redirects=False)
    plano = db.scalar(select(Plano))
    assert (resposta.status_code, resposta.headers["location"]) == (303, f"/planos/{plano.id}")
    assert plano.valor_total == Decimal("712.50")

    # 4. Cobrança Pix na AbacatePay falsa
    api_falsa.responder("/transparents/create", COBRANCA)
    api_falsa.responder("/transparents/list", [COBRANCA])
    resposta = client.post(f"/planos/{plano.id}/pagamento", data={"forma": "pix"}, follow_redirects=False)
    assert (resposta.status_code, resposta.headers["location"]) == (303, f"/planos/{plano.id}/cobranca")
    assert client.get(f"/planos/{plano.id}/cobranca").status_code == 200

    # 5. Webhook assinado confirma o pagamento
    resposta = _webhook_pagamento_confirmado(client, COBRANCA["id"])
    assert (resposta.status_code, resposta.json()) == (200, {"mensagem": "confirmado"})

    # 6. Plano ativo
    db.expire_all()
    assert db.scalar(select(Pagamento.status)) == StatusPagamento.CONFIRMADO
    assert set(db.scalars(select(Sessao.status))) == {StatusSessao.AGENDADA}
    assert "Pagamento confirmado" in client.get(f"/planos/{plano.id}").text
    assert emails == [(EMAIL, "Pagamento confirmado — plano de atendimento")]
