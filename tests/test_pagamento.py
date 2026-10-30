import logging
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from app.models.cliente import Cliente
from app.models.pagamento import FormaPagamento, Pagamento, StatusPagamento
from app.models.sessao import StatusSessao
from app.routers import cliente as cliente_router
from app.routers import inicio as inicio_router
from app.services import abacatepay_client, pagamento_service
from app.services.abacatepay_client import centavos
from tests.abacatepay_falsa import (
    BR_CODE,
    BR_CODE_ALTERNATIVO,
    CHAVE_API_TESTE,
    CODIGO_BARRAS,
    IDENTIFICATION_NUMBER,
    LINHA_DIGITAVEL,
    QR_CODE_BASE64,
    QR_CODE_BASE64_PURO,
    URL_BOLETO,
    cobranca_boleto,
    cobranca_pix,
    erro_caractere,
)
from tests.fabricas import (
    criar_cliente,
    criar_especialidade,
    criar_funcionario,
    criar_paciente,
    criar_pagamento,
    criar_plano,
    logar,
)

AGORA = datetime(2026, 8, 1, 10, 0)  # sábado, horário local
AGORA_UTC = datetime(2026, 8, 1, 13, 0, tzinfo=timezone.utc)
CRIADO_EM_UTC = datetime(2026, 8, 1, 12, 0)  # 09:00 local: o prazo da RN09 vence às 13:00 local
CPF_VALIDO = "529.982.247-25"
SEGUNDAS_DE_AGOSTO = [datetime(2026, 8, dia, 14, 0) for dia in (3, 10, 17, 24, 31)]
PENDENTE = StatusSessao.PENDENTE_PAGAMENTO


def _fixar_relogio(monkeypatch, agora_local, agora_utc=AGORA_UTC):
    for modulo in (cliente_router, inicio_router):
        monkeypatch.setattr(modulo, "agora_local", lambda: agora_local)
    monkeypatch.setattr(cliente_router, "agora_utc", lambda: agora_utc)


@pytest.fixture(autouse=True)
def relogio_fixo(monkeypatch):
    _fixar_relogio(monkeypatch, AGORA)


@pytest.fixture
def carla(db):
    return criar_funcionario(db, criar_especialidade(db, "Fonoaudiologia", "150.00"), email="carla@exemplo.com", nome="Carla Fono")


@pytest.fixture
def maria(db):
    usuario = criar_cliente(db, email="maria@exemplo.com", nome="Maria Souza")
    db.get(Cliente, usuario.id).cpf = CPF_VALIDO
    db.commit()
    return usuario


@pytest.fixture
def plano(db, carla, maria):
    pedro = criar_paciente(db, maria.id, nome="Pedro Souza")
    return criar_plano(
        db, pedro, carla, [(data, PENDENTE) for data in SEGUNDAS_DE_AGOSTO], criado_em=CRIADO_EM_UTC, valor_total="750.00"
    )


# Primeira sessão em 10/08: o boleto vence em 05/08 (3 dias úteis após o sábado 01/08) e é oferecido (RN09)
@pytest.fixture
def plano_boleto(db, carla, maria):
    pedro = criar_paciente(db, maria.id, nome="Pedro Souza")
    return criar_plano(
        db, pedro, carla, [(data, PENDENTE) for data in SEGUNDAS_DE_AGOSTO[1:]], criado_em=CRIADO_EM_UTC,
        valor_total="600.00",
    )


@pytest.fixture
def maria_logada(client, maria):
    logar(client, maria.email)
    return maria


def _pagar(client, plano, forma="pix"):
    return client.post(f"/planos/{plano.id}/pagamento", data={"forma": forma}, follow_redirects=False)


def _html(resposta) -> str:
    return " ".join(resposta.text.split())


def _codigo(html: str, id_: str) -> str | None:
    achado = re.search(rf'<p id="{id_}" class="cobranca-codigo[^"]*"[^>]*>([^<]*)</p>', html)
    return achado.group(1) if achado else None


# --- Conversão e regras puras ------------------------------------------------------------------

@pytest.mark.parametrize("valor, esperado", [
    ("750.00", 75000), ("0.10", 10), ("1234.56", 123456), ("1.00", 100), ("99999999.99", 9999999999),
])
def test_centavos_converte_decimal_sem_float(valor, esperado):
    resultado = centavos(Decimal(valor))
    assert resultado == esperado and type(resultado) is int


def test_expiracao_do_pix_e_o_restante_do_prazo_menos_60_segundos():
    assert pagamento_service.segundos_expiracao_pix(CRIADO_EM_UTC, AGORA) == 3 * 3600 - 60
    prazo = datetime(2026, 8, 1, 13, 0)
    assert pagamento_service.segundos_expiracao_pix(CRIADO_EM_UTC, prazo - timedelta(seconds=120)) == 60


def test_pix_e_recusado_quando_restam_menos_de_120_segundos():
    prazo = datetime(2026, 8, 1, 13, 0)
    with pytest.raises(pagamento_service.CobrancaNaoGerada, match="menos de 2 minutos"):
        pagamento_service.segundos_expiracao_pix(CRIADO_EM_UTC, prazo - timedelta(seconds=119))


@pytest.mark.parametrize("telefone, esperado", [
    ("(54) 99999-1234", "(54) 99999-1234"),
    ("54999991234", "(54) 99999-1234"),
    ("+55 54 99999-1234", "(54) 99999-1234"),
    ("(54) 3333-1234", "(54) 3333-1234"),
])
def test_celular_e_enviado_no_formato_da_documentacao(telefone, esperado):
    assert pagamento_service.formatar_celular(telefone) == esperado


@pytest.mark.parametrize("telefone", ["", None, "9999-1234", "123"])
def test_celular_sem_ddd_bloqueia_a_cobranca(telefone):
    with pytest.raises(pagamento_service.CobrancaNaoGerada, match="DDD"):
        pagamento_service.formatar_celular(telefone)


# --- Botões "Pagar agora" ----------------------------------------------------------------------

def test_inicio_e_pagina_do_plano_mostram_pagar_agora(client, plano, maria_logada):
    for url in ("/", f"/planos/{plano.id}"):
        assert f'href="/planos/{plano.id}/pagamento"' in client.get(url).text, url


def test_plano_pago_nao_mostra_pagar_agora(client, db, carla, maria_logada):
    pedro = criar_paciente(db, maria_logada.id)
    pago = criar_plano(db, pedro, carla, [(SEGUNDAS_DE_AGOSTO[0], StatusSessao.AGENDADA)], criado_em=CRIADO_EM_UTC)
    criar_pagamento(db, pago, confirmado_em=datetime(2026, 8, 1, 12, 30))
    for url in ("/", f"/planos/{pago.id}"):
        assert "Pagar agora" not in client.get(url).text, url


# --- Escolha da forma de pagamento -------------------------------------------------------------

def test_escolha_mostra_pix_boleto_valor_e_prazo(client, plano_boleto, maria_logada, api_falsa):
    resposta = client.get(f"/planos/{plano_boleto.id}/pagamento")
    html = _html(resposta)
    assert resposta.status_code == 200
    for trecho in ('value="pix"', 'value="boleto"', "R$ 600,00", "01/08/2026 às 13:00", "horário de Brasília"):
        assert trecho in html, trecho
    assert "<script" not in resposta.text.split("<main", 1)[1].split("</main>", 1)[0]
    assert api_falsa.requisicoes == []


def test_plano_de_outro_cliente_retorna_404(client, db, plano, api_falsa):
    logar(client, criar_cliente(db, email="outra@exemplo.com", nome="Outra Cliente").email)
    assert client.get(f"/planos/{plano.id}/pagamento").status_code == 404
    assert _pagar(client, plano).status_code == 404
    assert client.get(f"/planos/{plano.id}/cobranca").status_code == 404
    assert client.get("/planos/999/pagamento").status_code == 404
    assert db.query(Pagamento).count() == 0 and api_falsa.requisicoes == []


def test_pagamento_exige_login(client, plano):
    resposta = client.get(f"/planos/{plano.id}/pagamento", follow_redirects=False)
    assert resposta.status_code == 303 and resposta.headers["location"] == "/login"


def test_plano_fora_do_prazo_nao_pode_ser_pago(client, db, monkeypatch, plano, maria_logada, api_falsa):
    _fixar_relogio(monkeypatch, datetime(2026, 8, 1, 13, 1))

    resposta = client.get(f"/planos/{plano.id}/pagamento")
    assert resposta.status_code == 409
    assert "O prazo para pagar este plano terminou em 01/08/2026 às 13:00" in _html(resposta)

    resposta = _pagar(client, plano)
    assert resposta.status_code == 409 and "terminou em 01/08/2026 às 13:00" in _html(resposta)
    assert db.query(Pagamento).count() == 0 and api_falsa.requisicoes == []


def test_prazo_exato_ainda_aceita_o_pagamento(client, db, monkeypatch, plano_boleto, maria_logada, api_falsa):
    _fixar_relogio(monkeypatch, datetime(2026, 8, 1, 13, 0))
    api_falsa.responder("/transparents/create", cobranca_boleto())
    assert _pagar(client, plano_boleto, "boleto").status_code == 303


def test_plano_que_nao_aguarda_pagamento_mostra_mensagem(client, db, carla, maria_logada, api_falsa):
    pedro = criar_paciente(db, maria_logada.id)
    agendado = criar_plano(db, pedro, carla, [(SEGUNDAS_DE_AGOSTO[0], StatusSessao.AGENDADA)], criado_em=CRIADO_EM_UTC)
    resposta = client.get(f"/planos/{agendado.id}/pagamento")
    assert resposta.status_code == 409 and "não está aguardando pagamento" in resposta.text
    assert _pagar(client, agendado).status_code == 409
    assert api_falsa.requisicoes == []


def test_forma_invalida_reexibe_a_escolha(client, db, plano, maria_logada, api_falsa):
    resposta = _pagar(client, plano, "cartao")
    assert resposta.status_code == 400 and "Escolha a forma de pagamento: Pix ou boleto." in resposta.text
    assert db.query(Pagamento).count() == 0 and api_falsa.requisicoes == []


def test_rota_de_pagamento_esta_no_tratamento_de_formularios():
    from app.main import FORMULARIOS_POR_ROTA
    assert FORMULARIOS_POR_ROTA["/planos/{plano_id}/pagamento"] == "cliente/pagamento.html"


# --- Criação da cobrança -----------------------------------------------------------------------

def test_pix_envia_a_requisicao_no_formato_da_documentacao(client, db, plano, maria_logada, api_falsa):
    api_falsa.responder("/transparents/create", cobranca_pix())

    resposta = _pagar(client, plano, "pix")

    assert resposta.status_code == 303 and resposta.headers["location"] == f"/planos/{plano.id}/cobranca"
    [requisicao] = api_falsa.chamadas("/transparents/create")
    assert requisicao.method == "POST"
    assert str(requisicao.url) == "https://api.abacatepay.com/v2/transparents/create"
    assert requisicao.headers["Authorization"] == f"Bearer {CHAVE_API_TESTE}"
    [corpo] = api_falsa.corpos("/transparents/create")
    assert corpo["method"] == "PIX"
    dados = corpo["data"]
    assert dados["amount"] == 75000 and type(dados["amount"]) is int
    assert dados["expiresIn"] == 3 * 3600 - 60
    assert dados["customer"] == {
        "name": "Maria Souza", "email": "maria@exemplo.com", "taxId": "52998224725", "cellphone": "(54) 99999-1234",
    }
    assert dados["externalId"] == f"plano-{plano.id}"
    assert dados["metadata"] == {"plano_id": plano.id}
    assert 0 < len(dados["description"]) <= 500
    assert "dueDate" not in dados and "ensureSameTaxId" not in dados

    pagamento = db.query(Pagamento).one()
    assert (pagamento.plano_id, pagamento.forma_pagamento, pagamento.status) == (
        plano.id, FormaPagamento.PIX, StatusPagamento.PENDENTE,
    )
    assert pagamento.abacatepay_payment_id == "pix_char_teste123" and pagamento.confirmado_em is None


def test_vencimento_do_boleto_conta_da_data_local_de_criacao(client, db, monkeypatch, maria, carla, maria_logada, api_falsa):
    # 23:30 local de quarta 05/08 já é quinta 06/08 em UTC: contando do dia local, 3 dias úteis dão 10/08 (e não 11/08)
    _fixar_relogio(monkeypatch, datetime(2026, 8, 5, 23, 30), datetime(2026, 8, 6, 2, 30, tzinfo=timezone.utc))
    pedro = criar_paciente(db, maria.id, nome="Pedro Souza")
    plano = criar_plano(
        db, pedro, carla, [(data, PENDENTE) for data in SEGUNDAS_DE_AGOSTO[2:]], criado_em=datetime(2026, 8, 6, 2, 0),
        valor_total="750.00",
    )
    api_falsa.responder("/transparents/create", cobranca_boleto())

    assert _pagar(client, plano, "boleto").status_code == 303

    [corpo] = api_falsa.corpos("/transparents/create")
    assert corpo["method"] == "BOLETO"
    assert corpo["data"]["dueDate"] == "2026-08-10"
    assert "expiresIn" not in corpo["data"]
    assert corpo["data"]["amount"] == 75000 and corpo["data"]["customer"]["taxId"] == "52998224725"
    assert db.query(Pagamento).one().forma_pagamento == FormaPagamento.BOLETO


def test_cobranca_explica_o_que_acontece_depois_do_pagamento(client, plano, maria_logada, api_falsa):
    api_falsa.responder("/transparents/create", cobranca_pix())
    _pagar(client, plano)
    html = _html(client.get(f"/planos/{plano.id}/cobranca"))
    assert "Depois do pagamento" in html
    assert "As sessões passam a ficar confirmadas e você recebe um e-mail com as datas." in html


def test_pix_com_menos_de_2_minutos_nao_chama_a_api(client, db, monkeypatch, plano, maria_logada, api_falsa):
    _fixar_relogio(monkeypatch, datetime(2026, 8, 1, 12, 58, 30))
    resposta = _pagar(client, plano, "pix")
    assert resposta.status_code == 503 and "menos de 2 minutos" in resposta.text
    assert db.query(Pagamento).count() == 0 and api_falsa.requisicoes == []


def test_cliente_sem_celular_valido_recebe_mensagem(client, db, plano, maria_logada, api_falsa):
    db.get(Cliente, maria_logada.id).telefone = "9999-1234"
    db.commit()
    resposta = _pagar(client, plano)
    assert resposta.status_code == 503 and "telefone celular com DDD" in resposta.text
    assert api_falsa.requisicoes == []


@pytest.mark.parametrize("falha, mensagem", [
    (httpx.Response(401, json={"error": "Token de autenticação inválido ou ausente."}), abacatepay_client.MSG_CONFIGURACAO),
    (httpx.Response(403, json={"error": "Sem permissão"}), abacatepay_client.MSG_CONFIGURACAO),
    (httpx.Response(500, text="erro interno"), abacatepay_client.MSG_INDISPONIVEL),
    (httpx.Response(422, json={"error": "Property 'data.customer.cellphone' is missing"}), abacatepay_client.MSG_RECUSADA),
    (httpx.Response(200, text="não é json"), abacatepay_client.MSG_RECUSADA),
    (httpx.ReadTimeout("tempo esgotado"), abacatepay_client.MSG_INDISPONIVEL),
    (httpx.ConnectError("sem conexão"), abacatepay_client.MSG_INDISPONIVEL),
])
def test_falha_da_api_nao_cria_pagamento_nem_quebra_a_tela(
    client, db, plano, maria_logada, api_falsa, caplog, falha, mensagem
):
    api_falsa.responder("/transparents/create", falha)
    with caplog.at_level(logging.INFO):
        resposta = _pagar(client, plano)

    assert resposta.status_code == 503
    assert mensagem in resposta.text and 'value="pix"' in resposta.text
    assert db.query(Pagamento).count() == 0
    assert CHAVE_API_TESTE not in caplog.text


def test_chave_de_api_ausente_nao_tenta_a_rede(client, db, monkeypatch, plano, maria_logada, api_falsa):
    monkeypatch.setattr(abacatepay_client.settings, "ABACATEPAY_API_KEY", "")
    resposta = _pagar(client, plano)
    assert resposta.status_code == 503 and abacatepay_client.MSG_CONFIGURACAO in resposta.text
    assert api_falsa.requisicoes == [] and db.query(Pagamento).count() == 0


def test_cobranca_existente_e_reaproveitada_sem_duplicar(client, db, plano, maria_logada, api_falsa):
    api_falsa.responder("/transparents/create", cobranca_pix())
    _pagar(client, plano, "pix")

    segunda = _pagar(client, plano, "boleto")
    assert segunda.status_code == 303 and segunda.headers["location"] == f"/planos/{plano.id}/cobranca"
    escolha = client.get(f"/planos/{plano.id}/pagamento", follow_redirects=False)
    assert escolha.status_code == 303 and escolha.headers["location"] == f"/planos/{plano.id}/cobranca"

    assert len(api_falsa.chamadas("/transparents/create")) == 1
    assert db.query(Pagamento).one().forma_pagamento == FormaPagamento.PIX


def test_cobranca_sem_cache_e_consultada_na_api(client, db, monkeypatch, plano, maria_logada, api_falsa):
    criar_pagamento(db, plano, status=StatusPagamento.PENDENTE).abacatepay_payment_id = "pix_char_teste123"
    db.commit()
    api_falsa.responder("/transparents/list", [cobranca_pix()])

    resposta = client.get(f"/planos/{plano.id}/cobranca")
    assert resposta.status_code == 200 and BR_CODE in resposta.text
    [consulta] = api_falsa.chamadas("/transparents/list")
    assert consulta.method == "GET" and consulta.url.params["id"] == "pix_char_teste123"

    client.get(f"/planos/{plano.id}/cobranca")
    assert len(api_falsa.chamadas("/transparents/list")) == 1


def test_falha_ao_consultar_a_cobranca_mostra_aviso(client, db, plano, maria_logada, api_falsa):
    criar_pagamento(db, plano, status=StatusPagamento.PENDENTE).abacatepay_payment_id = "pix_char_teste123"
    db.commit()
    api_falsa.responder("/transparents/list", httpx.ConnectError("sem conexão"))

    resposta = client.get(f"/planos/{plano.id}/cobranca")
    assert resposta.status_code == 200
    assert pagamento_service.MSG_COBRANCA_INDISPONIVEL in resposta.text
    assert 'http-equiv="refresh"' in resposta.text


def test_cobranca_sem_pagamento_volta_para_a_escolha(client, plano, maria_logada):
    resposta = client.get(f"/planos/{plano.id}/cobranca", follow_redirects=False)
    assert resposta.status_code == 303 and resposta.headers["location"] == f"/planos/{plano.id}/pagamento"


# --- Tela de cobrança --------------------------------------------------------------------------

def test_tela_do_pix_tem_qr_code_copia_e_cola_e_atualizacao(client, plano, maria_logada, api_falsa):
    api_falsa.responder("/transparents/create", cobranca_pix())
    resposta = client.post(f"/planos/{plano.id}/pagamento", data={"forma": "pix"})

    html = _html(resposta)
    assert resposta.status_code == 200
    assert f'src="{QR_CODE_BASE64}"' in html
    assert _codigo(html, "pix-copia-cola") == BR_CODE and "<textarea" not in html
    assert "Clique no código para selecioná-lo e copie." in html
    assert '<meta http-equiv="refresh" content="15">' in html
    for trecho in ("R$ 750,00", "Prazo limite", "01/08/2026 às 13:00", "horário de Brasília", "Aguardando pagamento"):
        assert trecho in html, trecho
    assert "Copiar" not in html and "<script" not in resposta.text.split("<main", 1)[1].split("</main>", 1)[0]


def test_tela_do_boleto_tem_linha_digitavel_e_link(client, plano_boleto, maria_logada, api_falsa):
    api_falsa.responder("/transparents/create", cobranca_boleto())
    html = _html(client.post(f"/planos/{plano_boleto.id}/pagamento", data={"forma": "boleto"}))

    assert _codigo(html, "linha-digitavel") == LINHA_DIGITAVEL and "readonly" not in html
    assert f'href="{URL_BOLETO}" target="_blank" rel="noopener noreferrer"' in html
    assert "Pagamento via boleto" in html and "Pagamento via Boleto" not in html and "R$ 600,00" in html
    assert '<meta http-equiv="refresh" content="15">' in html


def test_tela_apos_confirmacao_nao_atualiza_mais(client, db, plano, maria_logada, api_falsa):
    pagamento = criar_pagamento(db, plano, confirmado_em=datetime(2026, 8, 1, 13, 5))
    pagamento.abacatepay_payment_id = "pix_char_teste123"
    db.commit()

    html = _html(client.get(f"/planos/{plano.id}/cobranca"))
    assert "Pagamento confirmado" in html and "01/08/2026 às 10:05" in html
    assert 'http-equiv="refresh"' not in html
    assert api_falsa.requisicoes == []


def test_tela_apos_o_prazo_nao_mostra_os_dados_de_pagamento(client, db, monkeypatch, plano, maria_logada, api_falsa):
    criar_pagamento(db, plano, status=StatusPagamento.PENDENTE).abacatepay_payment_id = "pix_char_teste123"
    db.commit()
    _fixar_relogio(monkeypatch, datetime(2026, 8, 1, 13, 30))

    html = _html(client.get(f"/planos/{plano.id}/cobranca"))
    assert "Prazo de pagamento encerrado" in html and BR_CODE not in html
    # Depois do prazo, só a conferência (rede de segurança do webhook) consulta a API, e nada é confirmado
    assert 'http-equiv="refresh"' not in html and [r.url.path for r in api_falsa.requisicoes] == ["/v2/transparents/list"]
    assert db.query(Pagamento).one().status == StatusPagamento.PENDENTE


def test_rede_real_fica_bloqueada_nos_testes(monkeypatch):
    monkeypatch.setattr(abacatepay_client.settings, "ABACATEPAY_API_KEY", "qualquer")
    with pytest.raises(RuntimeError, match="bloqueada"):
        abacatepay_client.consultar_cobranca("pix_char_teste123")


# --- Respostas reais da AbacatePay em Dev mode (correção da 4B) --------------------------------

@pytest.mark.parametrize("texto, esperado", [
    ("NeuroClinic — plano de Fonoaudiologia (João Conceição)", "NeuroClinic - plano de Fonoaudiologia (Joao Conceicao)"),
    ("Período – março", "Periodo - marco"),
    ("“Aspas” e ‘simples’", "'Aspas' e 'simples'"),
    ("linha\nnova\tcom\x00controle\x7f", "linha nova com controle"),
    ("  muitos    espaços  ", "muitos espacos"),
    ("Olá 😀 fim", "Ola fim"),
    (None, ""),
])
def test_sanear_texto_deixa_so_ascii(texto, esperado):
    resultado = abacatepay_client.sanear_texto(texto)
    assert resultado == esperado and resultado.isascii()


def test_sanear_texto_respeita_o_limite_de_500():
    assert len(abacatepay_client.sanear_texto("ã" * 800)) == 500


def test_descricao_com_travessao_e_acentos_vai_em_ascii(client, db, carla, maria_logada, api_falsa):
    joao = criar_paciente(db, maria_logada.id, nome="João Conceição")
    plano_joao = criar_plano(db, joao, carla, [(SEGUNDAS_DE_AGOSTO[0], PENDENTE)], criado_em=CRIADO_EM_UTC)
    api_falsa.responder("/transparents/create", cobranca_pix())

    assert _pagar(client, plano_joao).status_code == 303

    [corpo] = api_falsa.corpos("/transparents/create")
    assert corpo["data"]["description"] == "NeuroClinic - plano de Fonoaudiologia (Joao Conceicao)"


def test_simulador_recusa_descricao_fora_do_ascii_como_a_api_real(api_falsa, monkeypatch):
    api_falsa.responder("/transparents/create", cobranca_pix())
    monkeypatch.setattr(abacatepay_client, "sanear_texto", lambda texto, limite=500: texto)
    with pytest.raises(abacatepay_client.ErroAbacatePay):
        abacatepay_client.criar_cobranca("PIX", 75000, {"name": "Maria"}, 1, "NeuroClinic — plano", 600)
    assert len(api_falsa.chamadas("/transparents/create")) == 2


def test_caractere_recusado_no_nome_repete_uma_vez_em_ascii(client, db, plano, maria_logada, api_falsa):
    maria_logada.nome = "Maria Conceição"
    db.commit()
    api_falsa.recusar_nome_nao_ascii = True
    api_falsa.responder("/transparents/create", cobranca_pix())

    resposta = _pagar(client, plano)

    assert resposta.status_code == 303
    primeira, segunda = api_falsa.corpos("/transparents/create")
    assert primeira["data"]["customer"]["name"] == "Maria Conceição"
    assert segunda["data"]["customer"]["name"] == "Maria Conceicao"
    assert segunda["data"]["customer"]["taxId"] == "52998224725"
    assert db.query(Pagamento).count() == 1


def test_caractere_recusado_de_novo_mostra_mensagem_amigavel(client, db, plano, maria_logada, api_falsa):
    api_falsa.responder("/transparents/create", erro_caractere("customer.name", "ç"))

    resposta = _pagar(client, plano)

    assert resposta.status_code == 503 and abacatepay_client.MSG_RECUSADA in resposta.text
    assert "Disallowed" not in resposta.text and "U+" not in resposta.text
    assert len(api_falsa.chamadas("/transparents/create")) == 2
    assert db.query(Pagamento).count() == 0


def test_outro_erro_de_validacao_nao_repete(client, db, plano, maria_logada, api_falsa):
    api_falsa.responder("/transparents/create", httpx.Response(422, json={"error": "Property 'data.amount' is missing"}))
    assert _pagar(client, plano).status_code == 503
    assert len(api_falsa.chamadas("/transparents/create")) == 1


def test_log_do_erro_da_api_tem_status_e_error_sem_dados_sensiveis(
    client, db, plano, maria_logada, api_falsa, caplog
):
    erro = f"Invalid customer 529.982.247-25 / 52998224725 maria@exemplo.com token {CHAVE_API_TESTE}"
    api_falsa.responder("/transparents/create", httpx.Response(400, json={"data": None, "error": erro}))

    with caplog.at_level(logging.WARNING):
        resposta = _pagar(client, plano)

    registros = [r for r in caplog.records if r.name == abacatepay_client.logger.name and r.levelno == logging.WARNING]
    assert registros and "HTTP 400" in caplog.text and "Invalid customer" in caplog.text
    for sensivel in ("529.982.247-25", "52998224725", "maria@exemplo.com", CHAVE_API_TESTE):
        assert sensivel not in caplog.text, sensivel
    assert "Invalid customer" not in resposta.text and abacatepay_client.MSG_RECUSADA in resposta.text


def test_formatar_linha_digitavel_de_47_digitos():
    assert abacatepay_client.formatar_linha_digitavel(IDENTIFICATION_NUMBER) == LINHA_DIGITAVEL
    assert abacatepay_client.formatar_linha_digitavel("123") == "123"
    assert abacatepay_client.formatar_linha_digitavel(None) is None


def test_boleto_mostra_linha_digitavel_de_identification_number(client, plano_boleto, maria_logada, api_falsa):
    api_falsa.responder("/transparents/create", cobranca_boleto())
    html = _html(client.post(f"/planos/{plano_boleto.id}/pagamento", data={"forma": "boleto"}))

    assert "Linha digitável" in html and _codigo(html, "linha-digitavel") == LINHA_DIGITAVEL
    assert CODIGO_BARRAS not in html and IDENTIFICATION_NUMBER not in html
    assert BR_CODE_ALTERNATIVO not in html and "<img" not in html.split("<main", 1)[1]
    assert f'href="{URL_BOLETO}"' in html


def test_boleto_sem_identification_number_mostra_codigo_de_barras(client, plano_boleto, maria_logada, api_falsa):
    resposta_boleto = cobranca_boleto()
    del resposta_boleto["identificationNumber"]
    api_falsa.responder("/transparents/create", resposta_boleto)
    html = _html(client.post(f"/planos/{plano_boleto.id}/pagamento", data={"forma": "boleto"}))

    assert "Código de barras" in html and "Linha digitável" not in html
    assert _codigo(html, "linha-digitavel") == CODIGO_BARRAS


def test_boleto_reexibido_pela_consulta_usa_os_mesmos_campos(client, db, plano_boleto, maria_logada, api_falsa):
    pagamento = criar_pagamento(db, plano_boleto, status=StatusPagamento.PENDENTE)
    pagamento.forma_pagamento, pagamento.abacatepay_payment_id = FormaPagamento.BOLETO, "bole_teste123"
    db.commit()
    api_falsa.responder("/transparents/list", [cobranca_boleto()])

    html = _html(client.get(f"/planos/{plano_boleto.id}/cobranca"))
    assert _codigo(html, "linha-digitavel") == LINHA_DIGITAVEL and BR_CODE_ALTERNATIVO not in html


@pytest.mark.parametrize("base64_recebido", [QR_CODE_BASE64, QR_CODE_BASE64_PURO])
def test_qr_code_com_e_sem_prefixo(client, plano, maria_logada, api_falsa, base64_recebido):
    api_falsa.responder("/transparents/create", {**cobranca_pix(), "brCodeBase64": base64_recebido})
    html = client.post(f"/planos/{plano.id}/pagamento", data={"forma": "pix"}).text

    assert f'src="data:image/png;base64,{QR_CODE_BASE64_PURO}"' in html
    assert "base64,data:" not in html
