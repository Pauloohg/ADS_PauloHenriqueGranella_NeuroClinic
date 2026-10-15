from datetime import date, datetime
from decimal import Decimal

import pytest

from app.core.fuso import local_para_utc
from app.models.pagamento import StatusPagamento
from app.models.plano import StatusPlano
from app.models.sessao import StatusSessao
from app.routers import admin as admin_router
from app.services import funcionario_service, painel_service as servico
from tests.fabricas import (
    criar_admin,
    criar_cliente,
    criar_especialidade,
    criar_funcionario,
    criar_pagamento,
    criar_paciente,
    criar_plano,
    logar,
)

AGENDADA = StatusSessao.AGENDADA
PENDENTE = StatusSessao.PENDENTE_PAGAMENTO
REALIZADA = StatusSessao.REALIZADA

AGORA = datetime(2026, 10, 15, 10, 0)  # horário local
OUTUBRO = date(2026, 10, 1)
SETEMBRO = date(2026, 9, 1)
MEIO_DE_OUTUBRO_UTC = datetime(2026, 10, 10, 15, 0)


@pytest.fixture(autouse=True)
def relogio_fixo(monkeypatch):
    monkeypatch.setattr(admin_router, "agora_local", lambda: AGORA)


@pytest.fixture
def fono(db):
    return criar_funcionario(db, criar_especialidade(db, "Fonoaudiologia", "150.00"))


@pytest.fixture
def psico(db):
    return criar_funcionario(db, criar_especialidade(db, "Psicologia", "180.00"), email="psi@exemplo.com", nome="Paula Psi")


@pytest.fixture
def paciente(db):
    return criar_paciente(db, criar_cliente(db).id)


@pytest.fixture
def admin_logado(client, db):
    logar(client, criar_admin(db).email)
    return client


def _contratar(db, paciente, funcionario, valor, confirmado_em, status=StatusPagamento.CONFIRMADO, sessao=AGENDADA):
    plano = criar_plano(db, paciente, funcionario, [(datetime(2026, 10, 20, 9, 0), sessao)], valor_total=valor)
    criar_pagamento(db, plano, confirmado_em, status)
    return plano


def _html(resposta) -> str:
    return " ".join(resposta.text.split())


# --- Mês selecionado -----------------------------------------------------------------------------

@pytest.mark.parametrize("texto, esperado", [
    (None, (OUTUBRO, False)),
    ("", (OUTUBRO, False)),
    ("2026-09", (SETEMBRO, False)),
    ("2026-10", (OUTUBRO, False)),
    ("2025-12", (date(2025, 12, 1), False)),
    ("2026-11", (OUTUBRO, True)),  # futuro
    ("2027-01", (OUTUBRO, True)),
    ("2026-13", (OUTUBRO, True)),
    ("2026-00", (OUTUBRO, True)),
    ("2026-9", (OUTUBRO, True)),
    ("setembro", (OUTUBRO, True)),
    ("2026-09-01", (OUTUBRO, True)),
])
def test_resolver_mes(texto, esperado):
    assert servico.resolver_mes(texto, AGORA) == esperado


def test_limites_do_mes_em_utc():
    assert servico.limites_utc(OUTUBRO) == (datetime(2026, 10, 1, 3, 0), datetime(2026, 11, 1, 3, 0))


# --- Receita e variação --------------------------------------------------------------------------

def test_receita_do_mes_em_decimal(db, fono, paciente):
    _contratar(db, paciente, fono, "1620.00", MEIO_DE_OUTUBRO_UTC)
    _contratar(db, paciente, fono, "600.10", MEIO_DE_OUTUBRO_UTC)
    _contratar(db, paciente, fono, "300.00", datetime(2026, 9, 10, 15, 0))

    painel = servico.montar_painel(db, OUTUBRO, AGORA)

    assert painel.receita == Decimal("2220.10") and isinstance(painel.receita, Decimal)
    assert painel.planos_contratados == 2
    assert painel.receita_anterior == Decimal("300.00")


def test_pagamento_pendente_nao_conta(db, fono, paciente):
    _contratar(db, paciente, fono, "1620.00", None, status=StatusPagamento.PENDENTE, sessao=PENDENTE)
    painel = servico.montar_painel(db, OUTUBRO, AGORA)
    assert (painel.receita, painel.planos_contratados, painel.ranking) == (Decimal("0.00"), 0, [])
    assert painel.vazio


def test_limites_de_fuso_no_fim_e_no_inicio_do_mes(db, fono, paciente):
    _contratar(db, paciente, fono, "100.00", local_para_utc(datetime(2026, 9, 30, 23, 30)))  # 02:30 UTC de 01/10
    _contratar(db, paciente, fono, "200.00", local_para_utc(datetime(2026, 10, 1, 0, 30)))  # 03:30 UTC de 01/10

    assert servico.montar_painel(db, SETEMBRO, AGORA).receita == Decimal("100.00")
    assert servico.montar_painel(db, OUTUBRO, AGORA).receita == Decimal("200.00")


def test_variacao_com_mes_anterior_zerado_nao_divide_por_zero(db, fono, paciente):
    _contratar(db, paciente, fono, "1620.00", MEIO_DE_OUTUBRO_UTC)
    painel = servico.montar_painel(db, OUTUBRO, AGORA)
    assert painel.variacao == servico.Variacao(Decimal("1620.00"), None)


@pytest.mark.parametrize("atual, anterior, esperado", [
    ("1500.00", "1200.00", servico.Variacao(Decimal("300.00"), Decimal("25.0"))),
    ("900.00", "1200.00", servico.Variacao(Decimal("-300.00"), Decimal("-25.0"))),
    ("0.00", "0.00", servico.Variacao(Decimal("0.00"), None)),
    ("100.00", "300.00", servico.Variacao(Decimal("-200.00"), Decimal("-66.7"))),
])
def test_calcular_variacao(atual, anterior, esperado):
    assert servico.calcular_variacao(Decimal(atual), Decimal(anterior)) == esperado


def test_novos_clientes_pelo_mes_local(db):
    for email, criado_em_local in [
        ("a@exemplo.com", datetime(2026, 9, 30, 23, 30)),
        ("b@exemplo.com", datetime(2026, 10, 1, 0, 30)),
        ("c@exemplo.com", datetime(2026, 10, 31, 23, 30)),
    ]:
        usuario = criar_cliente(db, email=email)
        usuario.criado_em = local_para_utc(criado_em_local)
    criar_admin(db).criado_em = local_para_utc(datetime(2026, 10, 5, 9, 0))
    db.commit()

    assert servico.montar_painel(db, OUTUBRO, AGORA).novos_clientes == 2
    assert servico.montar_painel(db, SETEMBRO, AGORA).novos_clientes == 1


# --- Visão geral ---------------------------------------------------------------------------------

def test_visao_geral_atual(db, fono, psico, paciente):
    criar_plano(db, paciente, fono, [(datetime(2026, 10, 20, 9, 0), AGENDADA)])
    criar_plano(db, paciente, fono, [(datetime(2026, 10, 21, 9, 0), PENDENTE)], criado_em=datetime(2026, 10, 15, 12, 0))
    criar_plano(db, paciente, fono, [(datetime(2026, 8, 3, 9, 0), REALIZADA)], status_plano=StatusPlano.ENCERRADO)
    criar_cliente(db, email="outro@exemplo.com")
    funcionario_service.desativar(db, psico.usuario.id, AGORA)

    painel = servico.montar_painel(db, OUTUBRO, AGORA)
    assert (painel.total_clientes, painel.planos_ativos, painel.funcionarios_ativos) == (2, 1, 1)


# --- Ranking -------------------------------------------------------------------------------------

def test_ranking_por_quantidade_com_desempate_por_nome(db, paciente):
    fono = criar_especialidade(db, "Fonoaudiologia")
    nomes = ["Zeca", "Bruna", "Ana", "Caio", "Dora", "Eva"]
    funcionarios = {
        nome: criar_funcionario(db, fono, email=f"{nome.lower()}@exemplo.com", nome=nome) for nome in nomes
    }
    for nome, quantidade in {"Zeca": 3, "Bruna": 2, "Ana": 2, "Caio": 1, "Dora": 1, "Eva": 1}.items():
        for _ in range(quantidade):
            _contratar(db, paciente, funcionarios[nome], "100.00", MEIO_DE_OUTUBRO_UTC)

    ranking = servico.montar_painel(db, OUTUBRO, AGORA).ranking

    assert [(l.nome, l.planos, l.receita) for l in ranking] == [
        ("Zeca", 3, Decimal("300.00")),
        ("Ana", 2, Decimal("200.00")),
        ("Bruna", 2, Decimal("200.00")),
        ("Caio", 1, Decimal("100.00")),
        ("Dora", 1, Decimal("100.00")),
    ]


def test_ranking_inclui_funcionario_desativado_e_ignora_outros_meses(db, fono, psico, paciente):
    _contratar(db, paciente, fono, "300.00", MEIO_DE_OUTUBRO_UTC)
    _contratar(db, paciente, psico, "500.00", datetime(2026, 9, 10, 15, 0))
    funcionario_service.desativar(db, fono.usuario.id, AGORA.replace(year=2027))

    ranking = servico.montar_painel(db, OUTUBRO, AGORA).ranking
    assert [(l.nome, l.especialidade, l.ativo) for l in ranking] == [("Carlos Fono", "Fonoaudiologia", False)]


# --- Por especialidade ---------------------------------------------------------------------------

def test_especialidade_com_mais_clientes_em_destaque(db, fono, psico):
    criar_especialidade(db, "Psicopedagogia")
    for i in range(3):
        paciente = criar_paciente(db, criar_cliente(db, email=f"f{i}@exemplo.com").id)
        criar_plano(db, paciente, fono, [(datetime(2026, 10, 20, 9, 0), AGENDADA)])
    maria = criar_cliente(db, email="maria@exemplo.com")
    for nome in ["Filho 1", "Filho 2"]:  # mesmo Cliente com dois pacientes conta uma vez
        criar_plano(db, criar_paciente(db, maria.id, nome=nome), psico, [(datetime(2026, 10, 20, 9, 0), AGENDADA)])
    _contratar(db, criar_paciente(db, maria.id), psico, "900.00", MEIO_DE_OUTUBRO_UTC)

    linhas = {l.nome: l for l in servico.montar_painel(db, OUTUBRO, AGORA).especialidades}

    assert [n for n in linhas] == ["Fonoaudiologia", "Psicologia", "Psicopedagogia"]
    assert (linhas["Fonoaudiologia"].clientes_ativos, linhas["Fonoaudiologia"].destaque) == (3, True)
    assert (linhas["Psicologia"].clientes_ativos, linhas["Psicologia"].destaque) == (1, False)
    assert (linhas["Psicologia"].planos, linhas["Psicologia"].receita) == (1, Decimal("900.00"))
    assert (linhas["Psicopedagogia"].clientes_ativos, linhas["Psicopedagogia"].destaque) == (0, False)
    assert (linhas["Fonoaudiologia"].largura_clientes, linhas["Psicologia"].largura_clientes) == (100, 33)


def test_sem_clientes_ativos_nenhuma_especialidade_em_destaque(db, fono):
    assert not any(l.destaque for l in servico.montar_painel(db, OUTUBRO, AGORA).especialidades)


# --- Rotas ---------------------------------------------------------------------------------------

def test_tela_com_dados(admin_logado, db, fono, paciente):
    _contratar(db, paciente, fono, "1620.00", MEIO_DE_OUTUBRO_UTC)
    _contratar(db, paciente, fono, "1200.00", datetime(2026, 9, 10, 15, 0))

    html = _html(admin_logado.get("/admin/painel"))

    assert "Indicadores de outubro de 2026" in html
    assert "R$ 1.620,00" in html
    assert "+R$ 420,00 (+35,0%)" in html
    assert "Carlos Fono" in html
    assert 'href="/admin/painel?mes=2026-09"' in html
    assert "?mes=2026-11" not in html  # sem link para o mês futuro
    assert "Ainda não há pagamentos confirmados neste mês" not in html
    assert html.count("<script") == 1  # só o bundle do Bootstrap, sem JavaScript novo


def test_tela_com_mes_anterior_zerado_mostra_traco(admin_logado, db, fono, paciente):
    _contratar(db, paciente, fono, "1620.00", MEIO_DE_OUTUBRO_UTC)
    assert "+R$ 1.620,00 (—)" in _html(admin_logado.get("/admin/painel"))


def test_tela_vazia(admin_logado, db, fono):
    html = _html(admin_logado.get("/admin/painel?mes=2026-09"))
    assert "Ainda não há pagamentos confirmados neste mês" in html
    assert "R$ 0,00" in html
    assert "Indicadores de setembro de 2026" in html
    assert 'href="/admin/painel?mes=2026-10"' in html


@pytest.mark.parametrize("mes", ["2026-11", "abc", "2026-13"])
def test_tela_com_mes_invalido_ou_futuro_volta_para_o_atual(admin_logado, db, mes):
    resposta = admin_logado.get(f"/admin/painel?mes={mes}")
    html = _html(resposta)
    assert resposta.status_code == 200
    assert "Indicadores de outubro de 2026" in html
    assert "inválido ou ainda não chegou" in html


@pytest.mark.parametrize("perfil", ["cliente", "funcionario"])
def test_cliente_e_funcionario_nao_acessam_o_painel(client, db, fono, perfil):
    email = criar_cliente(db).email if perfil == "cliente" else fono.usuario.email
    logar(client, email)
    assert client.get("/admin/painel").status_code == 403


def test_painel_sem_login_redireciona(client):
    resposta = client.get("/admin/painel", follow_redirects=False)
    assert resposta.status_code == 303 and resposta.headers["location"] == "/login"
