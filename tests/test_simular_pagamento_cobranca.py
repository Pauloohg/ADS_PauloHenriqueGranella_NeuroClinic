from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from app.core.config import settings
from app.main import FORMULARIOS_POR_ROTA
from app.models.cliente import Cliente
from app.models.pagamento import Pagamento, StatusPagamento
from app.models.sessao import Sessao, StatusSessao
from app.routers import cliente as cliente_router
from app.services import pagamento_service
from tests.abacatepay_falsa import cobranca_boleto, cobranca_pix
from tests.fabricas import criar_cliente, criar_especialidade, criar_funcionario, criar_paciente, criar_plano, logar

INICIO = datetime(2026, 8, 3, 10, 0)  # segunda, horário local
CRIADO_EM_UTC = datetime(2026, 8, 3, 12, 0)  # 09:00 local: Pix até 13:00
SEGUNDAS = [datetime(2026, 8, dia, 14, 0) for dia in (17, 24, 31)]
BOTAO = "Simular pagamento (ambiente de teste)"
RESPOSTAS = {
    "pix": lambda status="PENDING", dev_mode=True: cobranca_pix(valor=45000, status=status, dev_mode=dev_mode),
    "boleto": lambda status="PENDING", dev_mode=True: cobranca_boleto(valor=45000, status=status, dev_mode=dev_mode),
}


class Relogio:
    def __init__(self):
        self.agora = INICIO

    def utc(self) -> datetime:
        return (self.agora + timedelta(hours=3)).replace(tzinfo=timezone.utc)


@pytest.fixture
def relogio(monkeypatch):
    relogio = Relogio()
    monkeypatch.setattr(cliente_router, "agora_local", lambda: relogio.agora)
    monkeypatch.setattr(cliente_router, "agora_utc", relogio.utc)
    return relogio


@pytest.fixture
def ligada(monkeypatch):
    monkeypatch.setattr(settings, "SIMULACAO_PAGAMENTO_HABILITADA", True)


@pytest.fixture(autouse=True)
def sem_email(monkeypatch):
    async def ignorar(*args):
        return True

    monkeypatch.setattr(pagamento_service, "enviar_email", ignorar)


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


def _gerar(client, db, plano, maria, api_falsa, forma="pix"):
    api_falsa.responder("/transparents/create", RESPOSTAS[forma]())
    logar(client, maria.email)
    assert client.post(f"/planos/{plano.id}/pagamento", data={"forma": forma}, follow_redirects=False).status_code == 303
    return db.scalar(select(Pagamento))


def _simular(client, plano):
    return client.post(f"/planos/{plano.id}/cobranca/simular-pagamento", follow_redirects=False)


def _preparar_simulacao(api_falsa, forma="pix", dev_mode=True):
    api_falsa.responder("/transparents/list", [RESPOSTAS[forma](dev_mode=dev_mode)])
    api_falsa.responder("/transparents/simulate-payment", RESPOSTAS[forma]("PAID", dev_mode))


def _simulacoes(api_falsa) -> int:
    return len(api_falsa.chamadas("/transparents/simulate-payment"))


def _consultas(api_falsa) -> int:
    return len(api_falsa.chamadas("/transparents/list"))


def _situacao(db, plano, pagamento) -> tuple:
    db.expire_all()
    sessoes = set(db.scalars(select(Sessao.status).where(Sessao.plano_id == plano.id)))
    return db.get(Pagamento, pagamento.id).status, sessoes


def _html(resposta) -> str:
    return " ".join(resposta.text.split())


# --- Configuração desligada (padrão) -----------------------------------------------------------

def test_padrao_e_desligada():
    assert type(settings).model_fields["SIMULACAO_PAGAMENTO_HABILITADA"].default is False


def test_desligada_sem_botao_e_rota_404(client, db, plano, maria, api_falsa, relogio):
    pagamento = _gerar(client, db, plano, maria, api_falsa)
    _preparar_simulacao(api_falsa)

    html = _html(client.get(f"/planos/{plano.id}/cobranca"))
    assert "Pague com Pix" in html and BOTAO not in html and "simular-pagamento" not in html

    resposta = _simular(client, plano)
    assert resposta.status_code == 404
    assert _simulacoes(api_falsa) == 0
    assert _situacao(db, plano, pagamento)[0] == StatusPagamento.PENDENTE


def test_desligada_404_mesmo_para_visitante(client, plano):
    assert _simular(client, plano).status_code == 404


# --- Ligada: Pix pendente ----------------------------------------------------------------------

def test_ligada_mostra_o_botao_na_cobranca_pix_pendente(client, db, plano, maria, api_falsa, relogio, ligada):
    _gerar(client, db, plano, maria, api_falsa)
    html = _html(client.get(f"/planos/{plano.id}/cobranca"))
    assert BOTAO in html
    assert f'<form method="post" action="/planos/{plano.id}/cobranca/simular-pagamento"' in html
    assert html.count("<script") == 1 and "bootstrap.bundle.min.js" in html  # só o Bootstrap do layout, sem JS novo


def test_ligada_chama_a_simulacao_redireciona_e_nao_grava_nada(client, db, plano, maria, api_falsa, relogio, ligada):
    pagamento = _gerar(client, db, plano, maria, api_falsa)
    _preparar_simulacao(api_falsa)

    resposta = _simular(client, plano)

    assert resposta.status_code == 303 and resposta.headers["location"] == f"/planos/{plano.id}/cobranca"
    [simulacao] = api_falsa.chamadas("/transparents/simulate-payment")
    assert simulacao.method == "POST" and simulacao.url.params["id"] == "pix_char_teste123"
    assert _situacao(db, plano, pagamento) == (StatusPagamento.PENDENTE, {StatusSessao.PENDENTE_PAGAMENTO})
    assert db.get(Pagamento, pagamento.id).confirmado_em is None


def test_cobranca_confirma_logo_depois_da_simulacao_pela_conferencia(client, db, plano, maria, api_falsa, relogio, ligada):
    pagamento = _gerar(client, db, plano, maria, api_falsa)
    _preparar_simulacao(api_falsa)
    assert _simular(client, plano).status_code == 303
    api_falsa.responder("/transparents/list", [RESPOSTAS["pix"]("PAID")])
    consultas_antes = _consultas(api_falsa)

    # Mesmo instante da geração da cobrança: sem a liberação única, o intervalo de 10 s impediria a conferência
    html = _html(client.get(f"/planos/{plano.id}/cobranca"))

    assert "Pagamento confirmado" in html and BOTAO not in html
    assert _consultas(api_falsa) == consultas_antes + 1
    assert _situacao(db, plano, pagamento) == (StatusPagamento.CONFIRMADO, {StatusSessao.AGENDADA})


def test_liberacao_da_conferencia_vale_uma_unica_vez(client, db, plano, maria, api_falsa, relogio, ligada):
    _gerar(client, db, plano, maria, api_falsa)
    _preparar_simulacao(api_falsa)  # a consulta continua PENDING (webhook e conferência ainda não viram o pagamento)
    assert _simular(client, plano).status_code == 303
    consultas_antes = _consultas(api_falsa)

    client.get(f"/planos/{plano.id}/cobranca")
    client.get(f"/planos/{plano.id}/cobranca")

    assert _consultas(api_falsa) == consultas_antes + 1
    assert pagamento_service._CONFERENCIA_IMEDIATA == set()


def test_sem_simulacao_o_intervalo_de_conferencia_continua_valendo(client, db, plano, maria, api_falsa, relogio, ligada):
    _gerar(client, db, plano, maria, api_falsa)
    api_falsa.responder("/transparents/list", [RESPOSTAS["pix"]("PAID")])
    client.get(f"/planos/{plano.id}/cobranca")
    assert _consultas(api_falsa) == 0


# --- Recusas -----------------------------------------------------------------------------------

def test_plano_de_outro_cliente_404(client, db, plano, maria, api_falsa, relogio, ligada):
    pagamento = _gerar(client, db, plano, maria, api_falsa)
    _preparar_simulacao(api_falsa)
    logar(client, criar_cliente(db, email="joao@exemplo.com").email)

    assert _simular(client, plano).status_code == 404
    assert _simulacoes(api_falsa) == 0
    assert _situacao(db, plano, pagamento)[0] == StatusPagamento.PENDENTE


def test_plano_inexistente_404(client, db, maria, ligada):
    logar(client, maria.email)
    assert client.post("/planos/999/cobranca/simular-pagamento", follow_redirects=False).status_code == 404


def test_visitante_vai_ao_login(client, plano, ligada):
    resposta = _simular(client, plano)
    assert resposta.status_code == 303 and resposta.headers["location"] == "/login"


def test_boleto_recusado_sem_botao(client, db, plano, maria, api_falsa, relogio, ligada):
    pagamento = _gerar(client, db, plano, maria, api_falsa, "boleto")
    _preparar_simulacao(api_falsa, "boleto")
    assert BOTAO not in _html(client.get(f"/planos/{plano.id}/cobranca"))

    resposta = _simular(client, plano)

    assert resposta.status_code == 409
    assert pagamento_service.MSG_SIMULACAO_SO_PIX in _html(resposta)
    assert _simulacoes(api_falsa) == 0
    assert _situacao(db, plano, pagamento)[0] == StatusPagamento.PENDENTE


def test_ja_confirmado_recusado(client, db, plano, maria, api_falsa, relogio, ligada):
    pagamento = _gerar(client, db, plano, maria, api_falsa)
    db.get(Pagamento, pagamento.id).status = StatusPagamento.CONFIRMADO
    db.commit()
    _preparar_simulacao(api_falsa)

    resposta = _simular(client, plano)

    assert resposta.status_code == 409
    assert pagamento_service.MSG_PLANO_PAGO in _html(resposta)
    assert _simulacoes(api_falsa) == 0


def test_sem_cobranca_gerada_recusado(client, db, plano, maria, api_falsa, ligada):
    logar(client, maria.email)
    resposta = _simular(client, plano)
    assert resposta.status_code == 409 and pagamento_service.MSG_SEM_COBRANCA in _html(resposta)
    assert _simulacoes(api_falsa) == 0


def test_prazo_do_pix_encerrado_recusado(client, db, plano, maria, api_falsa, relogio, ligada):
    _gerar(client, db, plano, maria, api_falsa)
    _preparar_simulacao(api_falsa)
    relogio.agora = datetime(2026, 8, 3, 13, 1)  # o Pix venceu às 13:00

    resposta = _simular(client, plano)

    assert resposta.status_code == 409 and pagamento_service.MSG_SIMULACAO_PRAZO in _html(resposta)
    assert _simulacoes(api_falsa) == 0


def test_cobranca_fora_do_dev_mode_nao_e_simulada(client, db, plano, maria, api_falsa, relogio, ligada):
    pagamento = _gerar(client, db, plano, maria, api_falsa)
    _preparar_simulacao(api_falsa, dev_mode=False)

    resposta = _simular(client, plano)

    assert resposta.status_code == 503 and pagamento_service.MSG_SIMULACAO_FALHOU in _html(resposta)
    assert _simulacoes(api_falsa) == 0
    assert _situacao(db, plano, pagamento)[0] == StatusPagamento.PENDENTE
    assert pagamento_service._CONFERENCIA_IMEDIATA == set()


def test_falha_da_api_mostra_mensagem_amigavel(client, db, plano, maria, api_falsa, relogio, ligada):
    _gerar(client, db, plano, maria, api_falsa)
    api_falsa.responder("/transparents/list", [RESPOSTAS["pix"]()])
    api_falsa.responder("/transparents/simulate-payment", httpx.Response(500, text="erro interno"))

    resposta = _simular(client, plano)

    assert resposta.status_code == 503 and pagamento_service.MSG_SIMULACAO_FALHOU in _html(resposta)
    assert "erro interno" not in resposta.text
    assert pagamento_service._CONFERENCIA_IMEDIATA == set()


# --- Formulário e service ----------------------------------------------------------------------

def test_rota_esta_em_formularios_por_rota():
    assert FORMULARIOS_POR_ROTA["/planos/{plano_id}/cobranca/simular-pagamento"] == "cliente/pagamento.html"


def test_simular_pagamento_dev_mode_recusa_antes_de_simular(api_falsa):
    _preparar_simulacao(api_falsa, dev_mode=False)
    with pytest.raises(pagamento_service.SimulacaoForaDoDevMode, match="não é de Dev mode"):
        pagamento_service.simular_pagamento_dev_mode("pix_char_teste123")
    assert _simulacoes(api_falsa) == 0


def test_simular_pagamento_dev_mode_devolve_a_cobranca_simulada(api_falsa):
    _preparar_simulacao(api_falsa)
    resultado = pagamento_service.simular_pagamento_dev_mode("pix_char_teste123")
    assert resultado.status == "PAID" and resultado.dev_mode
