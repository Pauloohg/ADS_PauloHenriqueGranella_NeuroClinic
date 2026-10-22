import json
import logging
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import BackgroundTasks
from sqlalchemy import select, update
from sqlalchemy.orm.attributes import set_committed_value

from app.models.cliente import Cliente
from app.models.pagamento import FormaPagamento, Pagamento, StatusPagamento
from app.models.plano import Plano
from app.models.sessao import Sessao, StatusSessao
from app.routers import cliente as cliente_router
from app.routers import inicio as inicio_router
from app.routers import webhooks as webhooks_router
from app.services import pagamento_service
from tests.abacatepay_falsa import CHAVE_API_TESTE, QR_CODE_BASE64, SECRET_WEBHOOK_TESTE, cobranca_boleto, cobranca_pix
from tests.fabricas import criar_cliente, criar_especialidade, criar_funcionario, criar_paciente, criar_plano, logar

INICIO = datetime(2026, 8, 3, 10, 0)  # segunda, horário local
CRIADO_EM_UTC = datetime(2026, 8, 3, 12, 0)  # 09:00 local: Pix até 13:00, boleto até 06/08
SEGUNDAS = [datetime(2026, 8, dia, 14, 0) for dia in (17, 24, 31)]
URL_WEBHOOK = f"/webhooks/abacatepay?webhookSecret={SECRET_WEBHOOK_TESTE}"
RESPOSTAS = {
    "pix": lambda status="PENDING": cobranca_pix(valor=45000, status=status),
    "boleto": lambda status="PENDING": cobranca_boleto(valor=45000, status=status),
}


class Relogio:
    def __init__(self):
        self.agora = INICIO

    def avancar(self, segundos: float) -> None:
        self.agora += timedelta(seconds=segundos)

    def utc(self) -> datetime:
        return (self.agora + timedelta(hours=3)).replace(tzinfo=timezone.utc)


@pytest.fixture
def relogio(monkeypatch):
    relogio = Relogio()
    for modulo in (cliente_router, inicio_router, webhooks_router):
        monkeypatch.setattr(modulo, "agora_local", lambda: relogio.agora)
    for modulo in (cliente_router, webhooks_router):
        monkeypatch.setattr(modulo, "agora_utc", relogio.utc)
    return relogio


@pytest.fixture
def emails(monkeypatch) -> list[tuple]:
    enviados = []

    async def registrar(destinatario, assunto, corpo):
        enviados.append((destinatario, assunto))
        return True

    monkeypatch.setattr(pagamento_service, "enviar_email", registrar)
    return enviados


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


def _gerar(client, db, plano, maria, api_falsa, forma):
    api_falsa.responder("/transparents/create", RESPOSTAS[forma]())
    logar(client, maria.email)
    resposta = client.post(f"/planos/{plano.id}/pagamento", data={"forma": forma}, follow_redirects=False)
    assert resposta.status_code == 303
    return db.scalar(select(Pagamento))


def _consultas(api_falsa) -> int:
    return len(api_falsa.chamadas("/transparents/list"))


def _situacao(db, plano, pagamento) -> tuple:
    db.expire_all()
    sessoes = set(db.scalars(select(Sessao.status).where(Sessao.plano_id == plano.id)))
    return db.get(Pagamento, pagamento.id).status, sessoes


def _html(resposta) -> str:
    return " ".join(resposta.text.split())


def _webhook(client, evento: dict):
    corpo = json.dumps(evento).encode()
    cabecalhos = {"Content-Type": "application/json", "X-Webhook-Signature": pagamento_service.calcular_assinatura(corpo)}
    return client.post(URL_WEBHOOK, content=corpo, headers=cabecalhos)


def _evento(cobranca_id: str) -> dict:
    return {"id": "log_teste", "event": "transparent.completed",
            "data": {"transparent": {"id": cobranca_id, "amount": 45000, "status": "PAID"}}}


# --- PAID confirma pelo mesmo caminho do webhook -----------------------------------------------

@pytest.mark.parametrize("forma", ["pix", "boleto"])
def test_conferencia_com_paid_confirma(client, db, plano, maria, api_falsa, relogio, emails, caplog, forma):
    pagamento = _gerar(client, db, plano, maria, api_falsa, forma)
    api_falsa.responder("/transparents/list", [RESPOSTAS[forma]("PAID")])
    relogio.avancar(10)

    with caplog.at_level(logging.WARNING):
        resposta = client.get(f"/planos/{plano.id}/cobranca")

    assert resposta.status_code == 200 and "Pagamento confirmado" in _html(resposta)
    assert _situacao(db, plano, pagamento) == (StatusPagamento.CONFIRMADO, {StatusSessao.AGENDADA})
    assert db.get(Pagamento, pagamento.id).confirmado_em == relogio.utc().replace(tzinfo=None)
    assert len(emails) == 1 and emails[0][0] == "maria@exemplo.com"
    assert "confirmado por conferência, webhook não chegou" in caplog.text
    assert _consultas(api_falsa) == 1


def test_conferencia_paid_depois_do_prazo_usa_a_regra_do_webhook(client, db, plano, maria, api_falsa, relogio, emails):
    pagamento = _gerar(client, db, plano, maria, api_falsa, "pix")
    api_falsa.responder("/transparents/list", [RESPOSTAS["pix"]("PAID")])
    relogio.agora = datetime(2026, 8, 3, 13, 30)  # Pix venceu às 13:00; horários livres: confirma (RN09 + RN01)

    assert "Pagamento confirmado" in _html(client.get(f"/planos/{plano.id}/cobranca"))
    assert _situacao(db, plano, pagamento)[0] == StatusPagamento.CONFIRMADO and len(emails) == 1


@pytest.mark.parametrize("status_api", ["PENDING", "EXPIRED", "CANCELLED", ""])
def test_conferencia_sem_paid_nao_confirma(client, db, plano, maria, api_falsa, relogio, emails, status_api):
    pagamento = _gerar(client, db, plano, maria, api_falsa, "pix")
    api_falsa.responder("/transparents/list", [RESPOSTAS["pix"](status_api)])
    relogio.avancar(10)

    html = _html(client.get(f"/planos/{plano.id}/cobranca"))

    assert f'src="{QR_CODE_BASE64}"' in html and "Aguardando pagamento" in html
    assert _situacao(db, plano, pagamento) == (StatusPagamento.PENDENTE, {StatusSessao.PENDENTE_PAGAMENTO})
    assert emails == [] and _consultas(api_falsa) == 1


# --- Falha da API não quebra a página ----------------------------------------------------------

@pytest.mark.parametrize("falha", [
    httpx.Response(500, text="erro interno"),
    httpx.Response(401, json={"error": "Token de autenticação inválido ou ausente."}),
    httpx.Response(400, json={"error": "Invalid customer maria@exemplo.com 529.982.247-25"}),
    httpx.ReadTimeout("demorou"),
    httpx.ConnectError("sem conexão"),
])
def test_falha_da_api_na_conferencia_nao_quebra_a_pagina(client, db, plano, maria, api_falsa, relogio, emails, caplog, falha):
    pagamento = _gerar(client, db, plano, maria, api_falsa, "pix")
    api_falsa.responder("/transparents/list", falha)
    relogio.avancar(10)

    with caplog.at_level(logging.WARNING):
        resposta = client.get(f"/planos/{plano.id}/cobranca")

    html = _html(resposta)
    assert resposta.status_code == 200 and f'src="{QR_CODE_BASE64}"' in html
    assert pagamento_service.MSG_COBRANCA_INDISPONIVEL not in html
    assert _situacao(db, plano, pagamento)[0] == StatusPagamento.PENDENTE and emails == []
    assert f"Conferência do pagamento {pagamento.id} não concluída" in caplog.text
    if isinstance(falha, httpx.Response):
        assert f"HTTP {falha.status_code}" in caplog.text
    for sensivel in ("maria@exemplo.com", "529.982.247-25", "52998224725", CHAVE_API_TESTE, SECRET_WEBHOOK_TESTE):
        assert sensivel not in caplog.text, sensivel


def test_conferencia_usa_timeout_curto(client, db, plano, maria, api_falsa, relogio):
    _gerar(client, db, plano, maria, api_falsa, "boleto")
    api_falsa.responder("/transparents/list", [RESPOSTAS["boleto"]()])
    relogio.avancar(10)
    client.get(f"/planos/{plano.id}/cobranca")
    [consulta] = api_falsa.chamadas("/transparents/list")
    assert consulta.extensions["timeout"] == {"connect": 1.0, "read": 2.0, "write": 1.0, "pool": 1.0}


def test_timeout_da_conferencia_cabe_no_rnf02():
    from app.core.config import TIMEOUT_CONFERENCIA

    assert TIMEOUT_CONFERENCIA.connect + TIMEOUT_CONFERENCIA.read <= 3
    assert None not in (TIMEOUT_CONFERENCIA.connect, TIMEOUT_CONFERENCIA.read, TIMEOUT_CONFERENCIA.write, TIMEOUT_CONFERENCIA.pool)


# --- Intervalo de 10 s -------------------------------------------------------------------------

def test_consulta_no_maximo_uma_vez_a_cada_10_segundos(client, db, plano, maria, api_falsa, relogio):
    _gerar(client, db, plano, maria, api_falsa, "pix")
    api_falsa.responder("/transparents/list", [RESPOSTAS["pix"]()])

    client.get(f"/planos/{plano.id}/cobranca")
    assert _consultas(api_falsa) == 0  # a cobrança acabou de ser criada pendente

    relogio.avancar(10)
    client.get(f"/planos/{plano.id}/cobranca")
    assert _consultas(api_falsa) == 1

    for _ in range(3):
        relogio.avancar(3)  # 13, 16 e 19 s
        client.get(f"/planos/{plano.id}/cobranca")
    assert _consultas(api_falsa) == 1

    relogio.avancar(1)  # 20 s
    client.get(f"/planos/{plano.id}/cobranca")
    assert _consultas(api_falsa) == 2


def test_intervalo_vale_tambem_depois_de_uma_falha(client, db, plano, maria, api_falsa, relogio):
    _gerar(client, db, plano, maria, api_falsa, "pix")
    api_falsa.responder("/transparents/list", httpx.ConnectError("sem conexão"))
    relogio.avancar(10)
    client.get(f"/planos/{plano.id}/cobranca")
    relogio.avancar(5)
    client.get(f"/planos/{plano.id}/cobranca")
    assert _consultas(api_falsa) == 1


# --- Webhook e conferência juntos --------------------------------------------------------------

def test_webhook_antes_da_conferencia_nao_duplica(client, db, plano, maria, api_falsa, relogio, emails):
    pagamento = _gerar(client, db, plano, maria, api_falsa, "boleto")
    api_falsa.responder("/transparents/list", [RESPOSTAS["boleto"]("PAID")])

    assert _webhook(client, _evento(pagamento.abacatepay_payment_id)).json() == {"mensagem": "confirmado"}
    relogio.avancar(10)
    assert "Pagamento confirmado" in _html(client.get(f"/planos/{plano.id}/cobranca"))

    assert _consultas(api_falsa) == 0  # já confirmado: não há o que conferir
    assert len(emails) == 1


def test_conferencia_antes_do_webhook_nao_duplica(client, db, plano, maria, api_falsa, relogio, emails):
    pagamento = _gerar(client, db, plano, maria, api_falsa, "pix")
    api_falsa.responder("/transparents/list", [RESPOSTAS["pix"]("PAID")])
    relogio.avancar(10)
    client.get(f"/planos/{plano.id}/cobranca")
    confirmado_em = db.get(Pagamento, pagamento.id).confirmado_em

    assert _webhook(client, _evento(pagamento.abacatepay_payment_id)).json() == {"mensagem": "ja_confirmado"}

    assert len(emails) == 1
    db.expire_all()
    assert db.get(Pagamento, pagamento.id).confirmado_em == confirmado_em


def test_confirmacao_simultanea_so_um_lado_confirma(db, plano, maria, relogio):
    # Simula o outro pedido (webhook ou conferência) confirmando entre a leitura e o UPDATE deste
    pagamento = Pagamento(
        plano_id=plano.id, forma_pagamento=FormaPagamento.BOLETO, status=StatusPagamento.PENDENTE,
        abacatepay_payment_id="bole_teste123",
    )
    db.add(pagamento)
    db.commit()
    assert pagamento.status == StatusPagamento.PENDENTE
    db.execute(update(Pagamento).where(Pagamento.id == pagamento.id).values(status=StatusPagamento.CONFIRMADO))
    db.commit()
    set_committed_value(pagamento, "status", StatusPagamento.PENDENTE)  # a leitura deste pedido ficou desatualizada
    tarefas = BackgroundTasks()

    resultado = pagamento_service.confirmar_pagamento(
        db, db.get(Plano, plano.id), pagamento, relogio.agora, relogio.utc().replace(tzinfo=None), tarefas,
    )

    assert resultado.situacao == "ja_confirmado" and tarefas.tasks == []
    assert pagamento.status == StatusPagamento.CONFIRMADO


# --- Webhook sem data.transparent.id -----------------------------------------------------------

def test_webhook_sem_id_registra_so_as_chaves_e_responde_200(client, db, plano, maria, api_falsa, relogio, emails, caplog):
    pagamento = _gerar(client, db, plano, maria, api_falsa, "boleto")
    evento = {
        "id": "log_formato_boleto", "event": "transparent.completed", "apiVersion": 2,
        "data": {
            "boleto": {"identifier": pagamento.abacatepay_payment_id, "amount": 45000, "status": "PAID"},
            "customer": {"name": "Maria Souza", "email": "maria@exemplo.com", "taxId": "529.982.247-25"},
            "payerInformation": {"method": "BOLETO", "BOLETO": {"name": "Maria Souza", "taxId": "529.***.247-**"}},
        },
    }

    with caplog.at_level(logging.WARNING):
        resposta = _webhook(client, evento)

    assert resposta.status_code == 200
    avisos = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    [aviso] = [a for a in avisos if "sem data.transparent.id" in a]
    for chave in ("apiVersion", "event", "boleto{amount, identifier, status}", "customer{email, name, taxId}", "BOLETO{name, taxId}"):
        assert chave in aviso, chave
    for valor in ("Maria Souza", "maria@exemplo.com", "529.982.247-25", "529.***.247-**", "45000", "PAID",
                  pagamento.abacatepay_payment_id, "log_formato_boleto", "BOLETO\"", SECRET_WEBHOOK_TESTE):
        assert valor not in caplog.text, valor
    assert _situacao(db, plano, pagamento)[0] == StatusPagamento.PENDENTE and emails == []


def test_descrever_chaves_lista_e_profundidade():
    descricao = pagamento_service._descrever_chaves({"a": [{"x": 1, "y": {"z": {"profundo": 1}}}], "b": "valor", "c": []})
    assert descricao == "{a[{x, y{z{profundo}}}], b, c[]}"
    assert pagamento_service._descrever_chaves({"a": {"b": {"c": {"d": {"e": 1}}}}}) == "{a{b{c{d}}}}"
