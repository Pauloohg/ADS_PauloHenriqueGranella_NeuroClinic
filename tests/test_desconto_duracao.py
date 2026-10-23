import re
from datetime import datetime, time, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.config import DESCONTO_POR_DURACAO
from app.models.cliente import Cliente
from app.models.pagamento import StatusPagamento
from app.models.plano import Plano
from app.models.sessao import StatusSessao
from app.routers import cliente as cliente_router
from app.routers import inicio as inicio_router
from app.services import pagamento_service, plano_service
from tests.abacatepay_falsa import cobranca_pix
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
CRIADO_EM_UTC = datetime(2026, 8, 1, 12, 0)
SEGUNDAS_DE_AGOSTO = [datetime(2026, 8, dia, 14, 0) for dia in (3, 10, 17, 24, 31)]
JULHO = [(datetime(2026, 7, dia, 14, 0), StatusSessao.REALIZADA) for dia in (6, 13, 20, 27)]


@pytest.fixture(autouse=True)
def relogio_fixo(monkeypatch):
    for modulo in (cliente_router, inicio_router):
        monkeypatch.setattr(modulo, "agora_local", lambda: AGORA)
    monkeypatch.setattr(cliente_router, "agora_utc", lambda: AGORA_UTC)


@pytest.fixture
def carla(db):
    return criar_funcionario(db, criar_especialidade(db, "Fonoaudiologia", "150.00"), email="carla@exemplo.com", nome="Carla Fono")


@pytest.fixture
def maria(db):
    usuario = criar_cliente(db, email="maria@exemplo.com", nome="Maria Souza")
    db.get(Cliente, usuario.id).cpf = "529.982.247-25"
    db.commit()
    return usuario


@pytest.fixture
def pedro(db, maria):
    return criar_paciente(db, maria.id, nome="Pedro Souza")


@pytest.fixture
def maria_logada(client, maria):
    logar(client, maria.email)
    return maria


def _html(resposta) -> str:
    return " ".join(resposta.text.split())


def _form(paciente, duracao="mensal", celulas=("0-14",)):
    return {"paciente_id": str(paciente.id), "celula": list(celulas), "frequencia_semanal": "1",
            "duracao": duracao, "observacao": ""}


def _plano_com_desconto(db, paciente, funcionario, percentual=5, desconto="37.50", total="712.50", sessoes=None):
    plano = criar_plano(
        db, paciente, funcionario, sessoes or [(d, StatusSessao.PENDENTE_PAGAMENTO) for d in SEGUNDAS_DE_AGOSTO],
        criado_em=CRIADO_EM_UTC, valor_total=total,
    )
    plano.desconto_percentual, plano.valor_desconto = percentual, Decimal(desconto)
    db.commit()
    return plano


# --- Regra pura (RN11) ---------------------------------------------------------------------------

def test_tabela_de_descontos_por_duracao():
    assert DESCONTO_POR_DURACAO == {"quinzenal": 0, "mensal": 5, "bimestral": 7, "anual": 10}


@pytest.mark.parametrize("bruto, duracao, esperado", [
    ("300.00", "quinzenal", (0, "0.00", "300.00")),
    ("750.00", "mensal", (5, "37.50", "712.50")),
    ("1350.00", "bimestral", (7, "94.50", "1255.50")),
    ("7800.00", "anual", (10, "780.00", "7020.00")),
])
def test_desconto_de_cada_duracao(bruto, duracao, esperado):
    percentual, desconto, total = plano_service.aplicar_desconto(Decimal(bruto), duracao)
    assert (percentual, desconto, total) == (esperado[0], Decimal(esperado[1]), Decimal(esperado[2]))


@pytest.mark.parametrize("bruto, duracao, desconto, total", [
    ("900.50", "mensal", "45.03", "855.47"),  # 45,025: ROUND_HALF_UP sobe (o arredondamento bancário daria 45,02)
    ("999.50", "mensal", "49.98", "949.52"),  # 49,975
    ("1350.90", "bimestral", "94.56", "1256.34"),  # 94,563
    ("1999.95", "anual", "200.00", "1799.95"),  # 199,995
])
def test_desconto_arredonda_centavos_para_cima_no_meio(bruto, duracao, desconto, total):
    _, valor_desconto, valor_total = plano_service.aplicar_desconto(Decimal(bruto), duracao)
    assert (valor_desconto, valor_total) == (Decimal(desconto), Decimal(total))
    assert valor_desconto + valor_total == Decimal(bruto)


def test_desconto_incide_sobre_as_sessoes_reais_com_feriado(db, carla):
    sim = plano_service.simular_plano(db, carla.usuario.id, [0], time(14, 0), 1, "anual", AGORA)
    assert sim.feriados_pulados and len(sim.sessoes) == sim.quantidade
    assert not set(sim.feriados_pulados) & {s.date() for s in sim.sessoes}
    assert sim.valor_bruto == Decimal("150.00") * sim.quantidade
    assert sim.desconto_percentual == 10
    assert sim.valor_desconto == (sim.valor_bruto / 10).quantize(Decimal("0.01"))
    assert sim.valor_total == sim.valor_bruto - sim.valor_desconto


# --- Simulação e contratação --------------------------------------------------------------------

def test_json_da_simulacao_quinzenal_nao_tem_desconto(client, carla, maria_logada):
    dados = client.get(f"/contratar/{carla.usuario.id}/simulacao?dias=0&horario=14:00&frequencia=1&duracao=quinzenal").json()
    assert (dados["desconto_percentual"], dados["valor_desconto"]) == (0, "R$ 0,00")
    assert dados["valor_bruto"] == dados["valor_total"] == "R$ 300,00"


def test_json_da_simulacao_anual_mostra_cheio_desconto_e_total(client, carla, maria_logada):
    dados = client.get(f"/contratar/{carla.usuario.id}/simulacao?dias=0&horario=14:00&frequencia=1&duracao=anual").json()
    bruto = Decimal("150.00") * dados["quantidade"]
    assert dados["desconto_percentual"] == 10
    assert dados["valor_bruto"] == f"R$ {bruto:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def test_tela_de_contratacao_mostra_a_tabela_de_descontos(client, carla, pedro, maria_logada):
    html = _html(client.get(f"/contratar/{carla.usuario.id}"))
    tabela = re.search(r'<div class="tabela-descontos" id="tabela-descontos">.*?</div>', html).group(0)
    for linha in ("Quinzenal: sem desconto", "Mensal: 5% de desconto", "Bimestral: 7% de desconto", "Anual: 10% de desconto"):
        assert linha in tabela, linha
    assert 'id="resumo-valor-desconto"' in html and 'id="resumo-valor-bruto"' in html


def test_resumo_mostra_valor_cheio_desconto_e_total(client, carla, pedro, maria_logada):
    html = _html(client.post(f"/contratar/{carla.usuario.id}", data=_form(pedro, "mensal")))
    assert "Valor cheio</dt><dd class=\"font-monospace-num\">R$ 750,00" in html
    assert "Desconto de 5%</dt><dd class=\"font-monospace-num texto-desconto\">− R$ 37,50" in html
    assert "R$ 712,50" in html


def test_resumo_quinzenal_nao_mostra_linha_de_desconto(client, carla, pedro, maria_logada):
    html = _html(client.post(f"/contratar/{carla.usuario.id}", data=_form(pedro, "quinzenal")))
    assert "R$ 300,00" in html and "Desconto de" not in html and "Valor cheio" not in html


def test_confirmacao_grava_desconto_e_total_recalculados(client, db, carla, pedro, maria_logada):
    dados = {**_form(pedro, "bimestral"), "valor_total": "1.00", "desconto_percentual": "90", "valor_desconto": "1000"}
    client.post(f"/contratar/{carla.usuario.id}/confirmar", data=dados)
    plano = db.query(Plano).one()
    assert (plano.desconto_percentual, plano.valor_desconto, plano.valor_total) == (7, Decimal("94.50"), Decimal("1255.50"))


# --- Exibição do plano contratado -----------------------------------------------------------------

def test_plano_antigo_com_desconto_zero_nao_mostra_linha_de_desconto(client, db, carla, pedro, maria_logada):
    plano = criar_plano(db, pedro, carla, [(d, StatusSessao.AGENDADA) for d in SEGUNDAS_DE_AGOSTO], valor_total="750.00")
    assert (plano.duracao, plano.desconto_percentual, plano.valor_desconto) == ("mensal", 0, Decimal("0"))
    pagina = _html(client.get(f"/planos/{plano.id}"))
    assert "R$ 750,00" in pagina and "Desconto de" not in pagina and "Valor cheio" not in pagina
    assert "de desconto" not in _html(client.get("/"))


def test_plano_com_desconto_aparece_no_card_na_pagina_e_no_pagamento(client, db, carla, pedro, maria_logada):
    plano = _plano_com_desconto(db, pedro, carla)
    assert "R$ 712,50 <span class=\"text-muted small\">(com 5% de desconto)</span>" in _html(client.get("/"))
    for url in (f"/planos/{plano.id}", f"/planos/{plano.id}/pagamento"):
        html = _html(client.get(url))
        assert "Valor cheio</dt><dd class=\"font-monospace-num\">R$ 750,00" in html, url
        assert "Desconto de 5%" in html and "− R$ 37,50" in html and "R$ 712,50" in html, url


def test_valor_enviado_a_abacatepay_e_o_total_com_desconto(client, db, carla, pedro, maria_logada, api_falsa):
    client.post(f"/contratar/{carla.usuario.id}/confirmar", data=_form(pedro, "mensal"))
    plano = db.query(Plano).one()
    api_falsa.responder("/transparents/create", cobranca_pix(valor=71250))

    client.post(f"/planos/{plano.id}/pagamento", data={"forma": "pix"})

    [corpo] = api_falsa.corpos("/transparents/create")
    assert corpo["data"]["amount"] == 71250
    assert corpo["data"]["description"].isascii()
    cobranca = _html(client.get(f"/planos/{plano.id}/cobranca"))
    assert "Desconto de 5%" in cobranca and "R$ 750,00" in cobranca and "R$ 712,50" in cobranca


def test_email_de_confirmacao_mostra_o_total_pago(db, carla, pedro):
    plano = _plano_com_desconto(db, pedro, carla)
    pagamento = criar_pagamento(db, plano, confirmado_em=CRIADO_EM_UTC, status=StatusPagamento.CONFIRMADO)
    _, corpo = pagamento_service.montar_email_confirmacao(db, pagamento)
    corpo = " ".join(corpo.split())
    assert "Total pago</td><td style=\"padding:6px 0;\">R$ 712,50 (com 5% de desconto)" in corpo


# --- Renovação (HU07) ------------------------------------------------------------------------------

def test_renovacao_recalcula_o_desconto_com_a_tabela_atual(client, db, monkeypatch, carla, pedro, maria_logada):
    encerrado = criar_plano(db, pedro, carla, JULHO, valor_total="600.00")
    monkeypatch.setitem(plano_service.DESCONTO_POR_DURACAO, "mensal", 12)

    html = _html(client.get(f"/planos/{encerrado.id}/renovar"))
    assert "Desconto de 12%" in html and "− R$ 90,00" in html and "R$ 660,00" in html

    form = re.search(r'<form method="post" action="([^"]+)" id="form-renovar-direto">(.*?)</form>', html, re.S)
    campos = re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', form.group(2))
    client.post(form.group(1), data={nome: valor for nome, valor in campos})

    novo = db.scalars(select(Plano).where(Plano.id != encerrado.id)).one()
    assert (novo.desconto_percentual, novo.valor_desconto, novo.valor_total) == (12, Decimal("90.00"), Decimal("660.00"))
    db.refresh(encerrado)
    assert (encerrado.desconto_percentual, encerrado.valor_total) == (0, Decimal("600.00"))
