import base64
import hashlib
import hmac
import json
import logging
from datetime import date, datetime, timezone

import pytest
from fastapi_mail import FastMail

from app.core.config import ABACATEPAY_CHAVE_PUBLICA_HMAC
from app.models.cliente import Cliente
from app.models.pagamento import FormaPagamento, Pagamento, StatusPagamento
from app.models.plano import Plano
from app.models.sessao import Sessao, StatusSessao
from app.routers import admin as admin_router
from app.routers import cliente as cliente_router
from app.routers import inicio as inicio_router
from app.routers import webhooks as webhooks_router
from app.services import admin_clientes_service, painel_service, pagamento_service, plano_service
from tests.abacatepay_falsa import SECRET_WEBHOOK_TESTE
from tests.fabricas import (
    criar_admin,
    criar_cliente,
    criar_especialidade,
    criar_funcionario,
    criar_paciente,
    criar_plano,
    criar_plano_com_sessao,
    logar,
)

AGORA = datetime(2026, 8, 1, 10, 0)  # horário local
AGORA_UTC = datetime(2026, 8, 1, 13, 0, tzinfo=timezone.utc)
CRIADO_EM_UTC = datetime(2026, 8, 1, 12, 0)  # prazo da RN09: 13:00 local
DEPOIS_DO_PRAZO = datetime(2026, 8, 1, 13, 20)  # ainda dentro da carência de remoção
SEGUNDAS_DE_AGOSTO = [datetime(2026, 8, dia, 14, 0) for dia in (3, 10, 17, 24, 31)]
COBRANCA_ID = "pix_char_mXq2Lk7vB9nR4sT8yW1zC6dF"
URL = f"/webhooks/abacatepay?webhookSecret={SECRET_WEBHOOK_TESTE}"


def _fixar_relogio(monkeypatch, agora_local, agora_utc=AGORA_UTC):
    for modulo in (cliente_router, inicio_router, webhooks_router, admin_router):
        monkeypatch.setattr(modulo, "agora_local", lambda: agora_local)
    for modulo in (cliente_router, webhooks_router):
        monkeypatch.setattr(modulo, "agora_utc", lambda: agora_utc)


@pytest.fixture(autouse=True)
def relogio_fixo(monkeypatch, api_falsa):
    _fixar_relogio(monkeypatch, AGORA)


@pytest.fixture
def emails(monkeypatch) -> list[tuple]:
    enviados = []

    async def registrar(destinatario, assunto, corpo):
        enviados.append((destinatario, assunto, corpo))
        return True

    monkeypatch.setattr(pagamento_service, "enviar_email", registrar)
    return enviados


@pytest.fixture
def fono(db):
    return criar_especialidade(db, "Fonoaudiologia", "150.00")


@pytest.fixture
def carla(db, fono):
    return criar_funcionario(db, fono, email="carla@exemplo.com", nome="Carla Fono")


@pytest.fixture
def maria(db):
    usuario = criar_cliente(db, email="maria@exemplo.com", nome="Maria Souza")
    db.get(Cliente, usuario.id).cpf = "529.982.247-25"
    db.commit()
    return usuario


@pytest.fixture
def plano(db, carla, maria):
    pedro = criar_paciente(db, maria.id, nome="Pedro Souza")
    return criar_plano(
        db, pedro, carla, [(data, StatusSessao.PENDENTE_PAGAMENTO) for data in SEGUNDAS_DE_AGOSTO],
        criado_em=CRIADO_EM_UTC, valor_total="750.00",
    )


@pytest.fixture
def pagamento(db, plano):
    pagamento = Pagamento(
        plano_id=plano.id, forma_pagamento=FormaPagamento.PIX, status=StatusPagamento.PENDENTE,
        abacatepay_payment_id=COBRANCA_ID,
    )
    db.add(pagamento)
    db.commit()
    return pagamento


def evento(tipo="transparent.completed", cobranca_id=COBRANCA_ID, valor=75000, evento_id="log_Qw8eR2tY4uI6oP1aS3dF5gH7"):
    return {
        "id": evento_id,
        "event": tipo,
        "apiVersion": 2,
        "devMode": True,
        "data": {
            "transparent": {
                "id": cobranca_id, "externalId": "plano-1", "amount": valor, "paidAmount": None, "platformFee": 80,
                "status": "PAID", "items": [], "methods": [], "frequency": "ONE_TIME", "coupons": [], "devMode": True,
                "customerId": "cust_aB3dE5fG7hJ9kL1mN3pQ5rS7", "createdAt": "2026-08-01T13:00:00.000Z",
                "updatedAt": "2026-08-01T13:00:05.000Z", "receiptUrl": None, "metadata": {"plano_id": 1},
            },
            "customer": {"id": "cust_aB3dE5fG7hJ9kL1mN3pQ5rS7", "name": "Maria Souza", "email": "maria@exemplo.com",
                         "taxId": "529.***.247-**"},
            "payerInformation": {"method": "PIX", "PIX": {"name": "Maria Souza", "taxId": "529.***.247-**",
                                                          "isSameAsCustomer": True}},
        },
    }


def assinar(corpo: bytes) -> str:
    return base64.b64encode(hmac.new(ABACATEPAY_CHAVE_PUBLICA_HMAC.encode(), corpo, hashlib.sha256).digest()).decode()


def enviar(client, dados=None, *, corpo: bytes | None = None, url=URL, assinatura: str | None = None):
    corpo = corpo if corpo is not None else json.dumps(dados if dados is not None else evento()).encode()
    cabecalhos = {"Content-Type": "application/json", "X-Webhook-Signature": assinatura or assinar(corpo)}
    return client.post(url, content=corpo, headers=cabecalhos)


def _status_sessoes(db, plano) -> set:
    db.expire_all()
    return {s.status for s in db.query(Sessao).filter(Sessao.plano_id == plano.id)}


# --- Autenticação do webhook -------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "/webhooks/abacatepay",
    "/webhooks/abacatepay?webhookSecret=errado",
    f"/webhooks/abacatepay?webhookSecret={SECRET_WEBHOOK_TESTE}x",
    "/webhooks/abacatepay?webhookSecret=",
])
def test_secret_errado_ou_ausente_retorna_401(client, db, pagamento, emails, url):
    assert enviar(client, url=url).status_code == 401
    assert db.get(Pagamento, pagamento.id).status == StatusPagamento.PENDENTE and emails == []


def test_sem_secret_configurado_todo_webhook_e_recusado(client, monkeypatch, pagamento):
    monkeypatch.setattr(pagamento_service.settings, "ABACATEPAY_WEBHOOK_SECRET", "")
    assert enviar(client, url="/webhooks/abacatepay?webhookSecret=").status_code == 401


@pytest.mark.parametrize("assinatura", ["errada", base64.b64encode(b"x" * 32).decode(), " "])
def test_assinatura_errada_retorna_401(client, db, pagamento, emails, assinatura):
    assert enviar(client, assinatura=assinatura).status_code == 401
    assert db.get(Pagamento, pagamento.id).status == StatusPagamento.PENDENTE and emails == []


def test_assinatura_ausente_retorna_401(client, db, pagamento):
    resposta = client.post(URL, content=json.dumps(evento()).encode(), headers={"Content-Type": "application/json"})
    assert resposta.status_code == 401


def test_assinatura_e_calculada_sobre_o_corpo_bruto(client, db, pagamento, emails):
    # Espaços e ordem de chaves diferentes da serialização do Python: reserializar o JSON mudaria a assinatura
    bruto = json.dumps(evento(), indent=3, ensure_ascii=False, sort_keys=True).encode("utf-8")
    reserializado = json.dumps(json.loads(bruto)).encode()
    assert bruto != reserializado

    assert enviar(client, corpo=bruto, assinatura=assinar(reserializado)).status_code == 401
    assert enviar(client, corpo=bruto, assinatura=assinar(bruto)).status_code == 200
    assert db.get(Pagamento, pagamento.id).status == StatusPagamento.CONFIRMADO


def test_assinatura_bate_com_o_exemplo_da_documentacao():
    corpo = b'{"id":"log_abc123xyz","event":"transparent.completed"}'
    esperado = base64.b64encode(hmac.new(ABACATEPAY_CHAVE_PUBLICA_HMAC.encode("utf-8"), corpo, hashlib.sha256).digest())
    assert pagamento_service.assinatura_valida(corpo, esperado.decode())
    assert not pagamento_service.assinatura_valida(corpo + b" ", esperado.decode())


def test_json_invalido_com_assinatura_valida_retorna_400(client, pagamento):
    assert enviar(client, corpo=b"{nao e json").status_code == 400


# --- Evento transparent.completed --------------------------------------------------------------

def test_evento_valido_confirma_pagamento_agenda_sessoes_e_envia_email(client, db, plano, pagamento, emails):
    resposta = enviar(client)

    assert resposta.status_code == 200
    db.expire_all()
    confirmado = db.get(Pagamento, pagamento.id)
    assert confirmado.status == StatusPagamento.CONFIRMADO
    assert confirmado.confirmado_em == AGORA_UTC.replace(tzinfo=None)
    assert _status_sessoes(db, plano) == {StatusSessao.AGENDADA}
    assert db.query(Sessao).filter(Sessao.plano_id == plano.id).count() == 5

    [(destinatario, assunto, corpo)] = emails
    assert destinatario == "maria@exemplo.com" and "Pagamento confirmado" in assunto
    texto = " ".join(corpo.split())
    for trecho in (
        "Pedro Souza", "Fonoaudiologia", "Carla Fono", "Segunda, às 14:00", "03/08/2026 a 31/08/2026", "R$ 750,00",
        "Pix", "01/08/2026 às 10:00", "5 sessões", "03/08/2026 (segunda), às 14:00", "31/08/2026 (segunda), às 14:00",
    ):
        assert trecho in texto, trecho


def test_reenvio_do_evento_e_idempotente(client, db, monkeypatch, plano, pagamento, emails):
    enviar(client)
    _fixar_relogio(monkeypatch, datetime(2026, 8, 1, 10, 5), datetime(2026, 8, 1, 13, 5, tzinfo=timezone.utc))

    resposta = enviar(client)

    assert resposta.status_code == 200
    db.expire_all()
    assert db.get(Pagamento, pagamento.id).confirmado_em == AGORA_UTC.replace(tzinfo=None)
    assert len(emails) == 1


def test_id_desconhecido_responde_200_e_registra_estorno_manual(client, db, pagamento, emails, caplog):
    with caplog.at_level(logging.ERROR):
        resposta = enviar(client, evento(cobranca_id="pix_char_inexistente", valor=12345))

    assert resposta.status_code == 200
    assert "estorno manual" in caplog.text and "pix_char_inexistente" in caplog.text and "R$ 123,45" in caplog.text
    assert db.get(Pagamento, pagamento.id).status == StatusPagamento.PENDENTE and emails == []


@pytest.mark.parametrize("tipo", ["transparent.refunded", "transparent.disputed", "checkout.completed", "payout.completed"])
def test_evento_irrelevante_e_ignorado_com_200(client, db, plano, pagamento, emails, tipo):
    assert enviar(client, evento(tipo=tipo)).status_code == 200
    assert db.get(Pagamento, pagamento.id).status == StatusPagamento.PENDENTE
    assert _status_sessoes(db, plano) == {StatusSessao.PENDENTE_PAGAMENTO} and emails == []


def test_evento_sem_dados_da_cobranca_responde_200(client, pagamento, caplog):
    with caplog.at_level(logging.ERROR):
        assert enviar(client, {"id": "log_1", "event": "transparent.completed", "data": None}).status_code == 200
    assert "estorno manual" in caplog.text


def test_erro_inesperado_responde_500_e_nao_altera_nada(client, db, monkeypatch, plano, pagamento, emails):
    def falhar(*args, **kwargs):
        raise RuntimeError("banco fora do ar")

    monkeypatch.setattr(pagamento_service, "_confirmar", falhar)
    assert enviar(client).status_code == 500
    db.expire_all()
    assert db.get(Pagamento, pagamento.id).status == StatusPagamento.PENDENTE
    assert _status_sessoes(db, plano) == {StatusSessao.PENDENTE_PAGAMENTO} and emails == []


def test_falha_no_envio_do_email_so_vai_para_o_log(client, db, monkeypatch, pagamento, caplog):
    async def smtp_fora_do_ar(self, mensagem):
        raise ConnectionError("SMTP fora do ar")

    monkeypatch.setattr(FastMail, "send_message", smtp_fora_do_ar)
    with caplog.at_level(logging.ERROR):
        resposta = enviar(client)

    assert resposta.status_code == 200
    assert db.get(Pagamento, pagamento.id).status == StatusPagamento.CONFIRMADO
    assert "Falha ao enviar e-mail para maria@exemplo.com" in caplog.text


# --- Pagamento depois do prazo (RN09 + RN01) ---------------------------------------------------

def test_pagamento_atrasado_com_horarios_livres_e_confirmado(client, db, monkeypatch, plano, pagamento, emails):
    _fixar_relogio(monkeypatch, DEPOIS_DO_PRAZO, datetime(2026, 8, 1, 16, 20, tzinfo=timezone.utc))

    assert enviar(client).status_code == 200

    assert db.get(Pagamento, pagamento.id).status == StatusPagamento.CONFIRMADO
    assert _status_sessoes(db, plano) == {StatusSessao.AGENDADA} and len(emails) == 1


def test_pagamento_atrasado_com_conflito_nao_confirma_e_registra(
    client, db, monkeypatch, fono, carla, plano, pagamento, emails, caplog
):
    # Depois do prazo, outro cliente reservou um dos horários (sessões sobrepostas, não só o mesmo início)
    criar_plano_com_sessao(
        db, carla.usuario.id, fono, StatusSessao.AGENDADA, data_hora=datetime(2026, 8, 17, 13, 30),
        criado_em=datetime(2026, 8, 1, 16, 10), email_cliente="joana@exemplo.com",
    )
    _fixar_relogio(monkeypatch, DEPOIS_DO_PRAZO, datetime(2026, 8, 1, 16, 20, tzinfo=timezone.utc))

    with caplog.at_level(logging.ERROR):
        resposta = enviar(client)

    assert resposta.status_code == 200
    assert "pagamento fora do prazo com conflito: estorno manual" in caplog.text.lower()
    assert COBRANCA_ID in caplog.text and "R$ 750,00" in caplog.text
    db.expire_all()
    assert db.get(Pagamento, pagamento.id).status == StatusPagamento.PENDENTE
    assert _status_sessoes(db, plano) == {StatusSessao.PENDENTE_PAGAMENTO} and emails == []


def test_pagamento_de_plano_ja_removido_pela_limpeza_registra_estorno(client, db, plano, pagamento, emails, caplog):
    assert plano_service.remover_planos_vencidos(db, datetime(2026, 8, 1, 14, 1)) == 1

    with caplog.at_level(logging.ERROR):
        resposta = enviar(client)

    assert resposta.status_code == 200
    assert "estorno manual" in caplog.text and COBRANCA_ID in caplog.text and "R$ 750,00" in caplog.text
    assert db.query(Plano).count() == 0 and emails == []


# --- Integração com as telas -------------------------------------------------------------------

def test_apos_o_webhook_o_inicio_mostra_o_plano_ativo(client, db, plano, pagamento, maria, emails):
    logar(client, maria.email)
    antes = " ".join(client.get("/").text.split())
    assert "Aguardando pagamento" in antes and "Pagar agora" in antes

    enviar(client)

    depois = " ".join(client.get("/").text.split())
    assert 'status-ativo">Ativo' in depois
    assert "Aguardando pagamento" not in depois and "Pagar agora" not in depois
    assert "0 de 5 sessões realizadas" in depois
    assert "Segunda 03/08, às 14:00" in depois


def test_apos_o_webhook_a_pagina_do_plano_mostra_forma_e_data(client, db, plano, pagamento, maria, emails):
    logar(client, maria.email)
    enviar(client)

    html = " ".join(client.get(f"/planos/{plano.id}").text.split())
    assert "Plano de atendimento" in html and 'status-ativo">Ativo' in html
    assert "<dt>Pagamento</dt><dd>Pix</dd>" in html.replace("> <", "><")
    assert "<dt>Confirmado em</dt><dd>01/08/2026 às 10:00</dd>" in html.replace("> <", "><")
    assert "Pagar agora" not in html

    cobranca = " ".join(client.get(f"/planos/{plano.id}/cobranca").text.split())
    assert "Pagamento confirmado" in cobranca and 'http-equiv="refresh"' not in cobranca


def test_apos_o_webhook_painel_e_rf14_refletem_o_plano(client, db, plano, pagamento, maria, emails):
    agosto = date(2026, 8, 1)
    antes = painel_service.montar_painel(db, agosto, AGORA)
    assert (antes.receita, antes.planos_contratados) == (0, 0)
    assert admin_clientes_service.listar(db, AGORA).clientes[0].planos_ativos == 0

    enviar(client)

    db.expire_all()
    painel = painel_service.montar_painel(db, agosto, AGORA)
    assert str(painel.receita) == "750.00" and painel.planos_contratados == 1 and painel.planos_ativos == 1
    assert [linha.nome for linha in painel.ranking] == ["Carla Fono"]
    [cliente] = admin_clientes_service.listar(db, AGORA).clientes
    assert cliente.id == maria.id and cliente.planos_ativos == 1
    [paciente] = admin_clientes_service.detalhar(db, maria.id, AGORA).pacientes
    assert [resumo.situacao for resumo in paciente.planos] == ["ativo"]

    logar(client, criar_admin(db).email)
    html = " ".join(client.get("/admin/painel?mes=2026-08").text.split())
    assert "R$ 750,00" in html


def test_limpeza_preserva_plano_pago_por_webhook(client, db, plano, pagamento, emails):
    enviar(client)
    assert plano_service.remover_planos_vencidos(db, datetime(2026, 12, 1, 10, 0)) == 0
    assert db.get(Plano, plano.id) is not None
    assert db.query(Sessao).filter(Sessao.plano_id == plano.id).count() == 5


def test_fluxo_completo_cobranca_webhook_e_tela(client, db, plano, maria, emails, api_falsa):
    from tests.abacatepay_falsa import cobranca_pix

    logar(client, maria.email)
    api_falsa.responder("/transparents/create", cobranca_pix(id_=COBRANCA_ID))
    tela = client.post(f"/planos/{plano.id}/pagamento", data={"forma": "pix"})
    assert "Aguardando pagamento" in tela.text

    assert enviar(client).status_code == 200

    html = " ".join(client.get(f"/planos/{plano.id}/cobranca").text.split())
    assert "Pagamento confirmado" in html and "01/08/2026 às 10:00" in html
