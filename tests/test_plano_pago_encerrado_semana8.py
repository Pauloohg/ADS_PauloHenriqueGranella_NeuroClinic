import re
from datetime import datetime, timezone

import pytest

from app.models.pagamento import FormaPagamento, Pagamento, StatusPagamento
from app.models.plano import StatusPlano
from app.models.sessao import StatusSessao
from app.routers import cliente as cliente_router
from app.services import contratacao_service
from tests.fabricas import criar_cliente, criar_especialidade, criar_funcionario, criar_paciente, criar_plano, logar

PENDENTE = StatusSessao.PENDENTE_PAGAMENTO
AGENDADA = StatusSessao.AGENDADA
REALIZADA = StatusSessao.REALIZADA
AGORA = datetime(2026, 8, 3, 10, 20)  # segunda, horário local
CRIADO = datetime(2026, 7, 28, 13, 0)


@pytest.fixture(autouse=True)
def relogio_fixo(monkeypatch):
    monkeypatch.setattr(cliente_router, "agora_local", lambda: AGORA)
    monkeypatch.setattr(cliente_router, "agora_utc", lambda: datetime(2026, 8, 3, 13, 20, tzinfo=timezone.utc))


@pytest.fixture
def carla(db):
    return criar_funcionario(db, criar_especialidade(db, "Psicopedagogia", "150.00"), email="carla@exemplo.com", nome="Carla Mendes")


@pytest.fixture
def pedro(client, db):
    maria = criar_cliente(db, email="maria@exemplo.com", nome="Maria Souza")
    logar(client, maria.email)
    return criar_paciente(db, maria.id, nome="Pedro Souza")


# Sessões passadas (realizada e agendada ainda sem registro), uma pendente esquisita e as futuras agendadas
SESSOES_PAGO = [
    (datetime(2026, 7, 31, 14, 0), REALIZADA),
    (datetime(2026, 8, 3, 9, 0), AGENDADA),  # já passou às 10:20
    (datetime(2026, 8, 7, 14, 0), AGENDADA),  # a próxima
    (datetime(2026, 8, 14, 14, 0), AGENDADA),
]


@pytest.fixture
def pago(db, pedro, carla):
    plano = criar_plano(db, pedro, carla, SESSOES_PAGO, criado_em=CRIADO)
    db.add(Pagamento(plano_id=plano.id, forma_pagamento=FormaPagamento.BOLETO, status=StatusPagamento.CONFIRMADO,
                     confirmado_em=datetime(2026, 7, 29, 13, 10)))  # UTC = 10:10 local
    db.commit()
    return plano


@pytest.fixture
def encerrado(db, pedro, carla):
    return criar_plano(db, pedro, carla, [(datetime(2026, 7, d, 14, 0), REALIZADA) for d in (6, 13, 20, 27)], criado_em=CRIADO)


def _html(resposta) -> str:
    assert resposta.status_code == 200, resposta.status_code
    return " ".join(resposta.text.split())


def _valor(html: str) -> str:
    return re.search(r'<aside class="cartao resumo-plano".*?</aside>', html).group(0)


# --- Próxima sessão: função pura (RNF14) ----------------------------------------------------------------

@pytest.mark.parametrize("agora, esperada", [
    (AGORA, datetime(2026, 8, 7, 14, 0)),
    (datetime(2026, 8, 3, 8, 0), datetime(2026, 8, 3, 9, 0)),
    (datetime(2026, 8, 7, 14, 0), datetime(2026, 8, 7, 14, 0)),  # exatamente no horário ainda é a próxima
    (datetime(2026, 8, 7, 14, 1), datetime(2026, 8, 14, 14, 0)),
    (datetime(2026, 8, 20, 9, 0), None),
])
def test_proxima_sessao_e_a_primeira_agendada_a_partir_de_agora(agora, esperada):
    assert contratacao_service.proxima_sessao(SESSOES_PAGO, agora) == esperada


def test_proxima_sessao_ignora_pendente_e_realizada_futuras():
    sessoes = [(datetime(2026, 8, 5, 9, 0), PENDENTE), (datetime(2026, 8, 6, 9, 0), REALIZADA),
               (datetime(2026, 8, 10, 9, 0), AGENDADA)]
    assert contratacao_service.proxima_sessao(sessoes, AGORA) == datetime(2026, 8, 10, 9, 0)


def test_texto_da_proxima_sessao():
    assert contratacao_service.texto_sessao(datetime(2026, 8, 7, 14, 0)) == "sexta, 07/08, às 14:00"
    assert contratacao_service.texto_sessao(datetime(2026, 9, 14, 9, 0)) == "segunda, 14/09, às 09:00"


# --- Plano pago ---------------------------------------------------------------------------------------

def test_plano_pago_com_bloco_verde(client, pago):
    html = _html(client.get(f"/planos/{pago.id}"))
    bloco = re.search(r'<section class="plano-pago" role="status" id="bloco-pago".*?</section>', html).group(0)
    assert '<p class="plano-bloco-titulo" id="titulo-pago">Pagamento confirmado</p>' in bloco
    assert "Recebido via Boleto em 29/07/2026, às 10:10." in bloco
    assert "Próxima sessão: <strong>sexta, 07/08, às 14:00</strong>." in bloco
    assert html.index('id="bloco-pago"') < html.index('class="lista-dados plano-dados"')
    assert "plano-encerrado" not in html and "Renovar" not in html


def test_plano_pago_destaca_a_proxima_sessao_na_lista(client, pago):
    html = _html(client.get(f"/planos/{pago.id}"))
    lista = re.search(r'<ol class="lista-sessoes">.*?</ol>', html).group(0)
    destaques = re.findall(r'<li class="plano-proxima" aria-current="date">(.*?)</li>', lista)
    assert len(destaques) == 1 and "07/08/2026" in destaques[0] and "próxima" in destaques[0]
    assert lista.count("<li") == 4  # lista aberta, com todas as datas, como antes
    assert "<details" not in lista


def test_forma_e_confirmacao_saem_do_cartao_valor(client, pago):
    valor = _valor(_html(client.get(f"/planos/{pago.id}")))
    assert "Confirmado em" not in valor and "<dt>Pagamento</dt>" not in valor and "Boleto" not in valor
    assert "<dt>Sessões</dt><dd>4</dd>" in valor and "Valor total" in valor


def test_plano_ativo_sem_proxima_nao_mostra_a_linha(client, db, pedro, carla, monkeypatch):
    plano = criar_plano(db, pedro, carla, [(datetime(2026, 8, 3, 9, 0), AGENDADA)], criado_em=CRIADO)
    html = _html(client.get(f"/planos/{plano.id}"))
    assert 'id="bloco-pago"' in html and "Próxima sessão" not in html and "plano-proxima" not in html


# --- Plano encerrado ----------------------------------------------------------------------------------

def test_plano_encerrado_com_bloco_cinza_e_renovar(client, encerrado):
    html = _html(client.get(f"/planos/{encerrado.id}"))
    bloco = re.search(r'<section class="plano-encerrado" id="bloco-encerrado".*?</section>', html).group(0)
    assert '<p class="plano-bloco-titulo" id="titulo-encerrado">Plano encerrado</p>' in bloco
    assert "A última sessão foi em 27/07/2026." in bloco
    assert f'<a href="/planos/{encerrado.id}/renovar" class="btn btn-primary">' in bloco and "Renovar plano" in bloco
    assert html.count("/renovar") == 1  # o "Renovar" do cartão Valor saiu
    assert "bloco-pago" not in html and "plano-proxima" not in html


def test_situacao_continua_derivada_sem_estado_novo(client, db, encerrado, pedro, carla):
    # A coluna Plano.status não é lida: "encerrado" vem das sessões
    assert encerrado.status == StatusPlano.ATIVO
    assert 'id="bloco-encerrado"' in _html(client.get(f"/planos/{encerrado.id}"))
    # Sessão passada ainda aguardando pagamento: não é encerrado
    aguardando = criar_plano(db, pedro, carla, [(datetime(2026, 8, 3, 9, 0), PENDENTE)], criado_em=datetime(2026, 8, 3, 12, 0))
    html = _html(client.get(f"/planos/{aguardando.id}"))
    assert "bloco-encerrado" not in html and "Renovar" not in html


def test_colunas_do_plano_nao_mudaram():
    from app.models.plano import Plano
    from app.models.sessao import Sessao
    assert {c.name for c in Plano.__table__.columns} == {
        "id", "paciente_id", "funcionario_id", "especialidade_id", "dias_semana", "horario", "frequencia_semanal",
        "duracao", "valor_total", "desconto_percentual", "valor_desconto", "data_inicio", "data_fim", "status",
        "observacao", "criado_em",
    }
    assert {c.name for c in Sessao.__table__.columns} == {"id", "plano_id", "data_hora", "status", "foi_remarcada"}


def test_plano_encerrado_nao_mostra_aviso_de_feriado_inventado(client, encerrado):
    # Decisão do bloco: a página do plano não mostrava o aviso de feriado, e ele não foi criado aqui
    assert "Feriado" not in _html(client.get(f"/planos/{encerrado.id}"))
