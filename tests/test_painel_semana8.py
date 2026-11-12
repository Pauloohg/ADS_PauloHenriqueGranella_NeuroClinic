import re
from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import event

from app.core.templating import moeda_fixa
from app.models.sessao import StatusSessao
from app.routers import admin as admin_router
from app.services import painel_service as servico
from app.services.painel_service import LinhaRanking
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

AGORA = datetime(2026, 11, 10, 10, 0)  # horário local
NOVEMBRO, OUTUBRO, SETEMBRO = date(2026, 11, 1), date(2026, 10, 1), date(2026, 9, 1)
EM_OUTUBRO_UTC = datetime(2026, 10, 10, 15, 0)
EM_SETEMBRO_UTC = datetime(2026, 9, 10, 15, 0)
EM_NOVEMBRO_UTC = datetime(2026, 11, 5, 15, 0)
SQL_ANTES = 10  # medido no Painel anterior a este bloco, com poucos e muitos dados


@pytest.fixture(autouse=True)
def relogio_fixo(monkeypatch):
    monkeypatch.setattr(admin_router, "agora_local", lambda: AGORA)


@pytest.fixture
def admin(client, db):
    logar(client, criar_admin(db).email)
    return client


@pytest.fixture
def fono(db):
    return criar_funcionario(db, criar_especialidade(db, "Fonoaudiologia", "150.00"), email="carlos@exemplo.com", nome="Carlos Lima")


@pytest.fixture
def psico(db):
    return criar_funcionario(db, criar_especialidade(db, "Psicologia", "200.00"), email="paula@exemplo.com", nome="Paula Ramos")


@pytest.fixture
def paciente(db):
    return criar_paciente(db, criar_cliente(db).id)


def _contratar(db, paciente, funcionario, valor, confirmado_em):
    plano = criar_plano(db, paciente, funcionario, [(datetime(2026, 11, 20, 9, 0), AGENDADA)], valor_total=valor)
    criar_pagamento(db, plano, confirmado_em)
    return plano


def _html(resposta) -> str:
    assert resposta.status_code == 200
    return " ".join(resposta.text.split())


def _receita(html: str) -> str:
    return html[html.index('id="titulo-receita"'):html.index('id="titulo-visao-geral"')]


# --- Comparação com o mês anterior (pura) ---------------------------------------------------------

def test_mes_fechado_com_a_conta_da_variacao():
    c = servico.comparar_com_anterior(OUTUBRO, NOVEMBRO, AGORA, Decimal("2856.00"), Decimal("4789.80"))
    # (2856,00 − 4789,80) / 4789,80 = −40,37…% → 40,4%
    assert c == servico.Comparacao("fechado", "40,4% a menos que setembro.", None, 60, 100)
    c = servico.comparar_com_anterior(OUTUBRO, NOVEMBRO, AGORA, Decimal("1500.00"), Decimal("1200.00"))
    assert (c.tipo, c.texto, c.largura_mes, c.largura_anterior) == ("fechado", "25,0% a mais que setembro.", 100, 80)


def test_mes_igual_ao_anterior():
    c = servico.comparar_com_anterior(OUTUBRO, NOVEMBRO, AGORA, Decimal("900.00"), Decimal("900.00"))
    assert (c.tipo, c.texto) == ("fechado", "Igual a setembro.")


@pytest.mark.parametrize("receita", ["0.00", "1140.00"])
def test_mes_em_andamento_sem_porcentagem(receita):
    c = servico.comparar_com_anterior(NOVEMBRO, NOVEMBRO, AGORA, Decimal(receita), Decimal("2856.00"))
    assert c == servico.Comparacao("andamento", "Outubro inteiro: R$ 2.856,00.", "10/11")
    assert "%" not in c.texto


def test_mes_sem_pagamentos():
    c = servico.comparar_com_anterior(OUTUBRO, NOVEMBRO, AGORA, Decimal("0.00"), Decimal("500.00"))
    assert c == servico.Comparacao("vazio", "Nenhum pagamento confirmado neste mês.")


def test_mes_anterior_sem_receita_nao_tem_porcentagem():
    c = servico.comparar_com_anterior(OUTUBRO, NOVEMBRO, AGORA, Decimal("700.00"), Decimal("0.00"))
    assert c == servico.Comparacao("sem_anterior", "Nenhum pagamento confirmado em setembro.", None, 100, 0)


def test_janeiro_compara_com_dezembro_do_ano_anterior():
    c = servico.comparar_com_anterior(date(2026, 1, 1), NOVEMBRO, AGORA, Decimal("110.00"), Decimal("100.00"))
    assert c.texto == "10,0% a mais que dezembro."


@pytest.mark.parametrize("atual, esperado, texto", [
    ("449.00", Decimal("12.3"), "12,3% a mais que setembro."),  # +12,25% exato: ROUND_HALF_UP sobe (HALF_EVEN daria 12,2)
    ("351.00", Decimal("-12.3"), "12,3% a menos que setembro."),  # −12,25% exato
    ("400.60", Decimal("0.2"), "0,2% a mais que setembro."),  # +0,15% exato
])
def test_arredondamento_meio_exato_round_half_up(atual, esperado, texto):
    assert servico.calcular_variacao(Decimal(atual), Decimal("400.00")).percentual == esperado
    assert servico.comparar_com_anterior(OUTUBRO, NOVEMBRO, AGORA, Decimal(atual), Decimal("400.00")).texto == texto


def test_moeda_fixa_usa_espaco_nao_separavel():
    assert moeda_fixa(Decimal("2845.80")) == "R$ 2.845,80"


# --- Comparação na tela ----------------------------------------------------------------------------

def test_tela_mes_fechado_com_duas_barras(admin, db, fono, paciente):
    _contratar(db, paciente, fono, "2856.00", EM_OUTUBRO_UTC)
    _contratar(db, paciente, fono, "4789.80", EM_SETEMBRO_UTC)
    resposta = admin.get("/admin/painel?mes=2026-10")
    assert '<p class="painel-receita-valor font-monospace-num">R$ 2.856,00</p>' in resposta.text
    bloco = _receita(_html(resposta))
    assert "Valor cobrado dos pagamentos confirmados, já com desconto." in bloco
    assert '<dt>Outubro</dt> <dd class="painel-trilho" aria-hidden="true"><span class="painel-barra-mes" style="width: 60%">' in bloco
    assert '<dt>Setembro</dt> <dd class="painel-trilho" aria-hidden="true"><span class="painel-barra-anterior" style="width: 100%">' in bloco
    assert "40,4% a menos que setembro." in bloco
    assert "Mês em andamento" not in _html(resposta)


def test_tela_mes_em_andamento(admin, db, fono, paciente):
    _contratar(db, paciente, fono, "1140.00", EM_NOVEMBRO_UTC)
    _contratar(db, paciente, fono, "2856.00", EM_OUTUBRO_UTC)
    html = _html(admin.get("/admin/painel"))
    assert '<p class="painel-andamento" id="mes-em-andamento">Mês em andamento, até 10/11</p>' in html
    bloco = _receita(html)
    assert "Outubro inteiro: R$ 2.856,00." in bloco
    assert "%" not in bloco and "painel-barra" not in bloco


def test_tela_mes_sem_pagamentos(admin, db, fono):
    html = _html(admin.get("/admin/painel?mes=2026-08"))
    bloco = _receita(html)
    assert "Nenhum pagamento confirmado neste mês." in bloco and "painel-barra" not in bloco
    assert "Nenhum plano contratado em agosto." in html


def test_tela_mes_com_anterior_sem_receita(admin, db, fono, paciente):
    _contratar(db, paciente, fono, "700.00", EM_OUTUBRO_UTC)
    bloco = _receita(_html(admin.get("/admin/painel?mes=2026-10")))
    assert "Nenhum pagamento confirmado em setembro." in bloco
    assert 'style="width: 0%"' in bloco and "%</" not in bloco and "a mais" not in bloco


def test_cartoes_menores_no_singular_e_plural(admin, db, fono, paciente):
    _contratar(db, paciente, fono, "100.00", EM_OUTUBRO_UTC)
    html = _html(admin.get("/admin/painel?mes=2026-10"))
    assert "1</strong> <span>plano contratado, com pagamento confirmado no mês</span>" in html
    assert "0</strong> <span>clientes novos, com conta criada no mês</span>" in html


# --- Navegação de mês ------------------------------------------------------------------------------

def test_navegacao_com_os_meses_vizinhos(admin, db):
    html = _html(admin.get("/admin/painel?mes=2026-10"))
    assert 'href="/admin/painel?mes=2026-09" class="btn btn-outline-primary btn-sm" rel="prev"><span aria-hidden="true">‹ </span>Setembro</a>' in html
    assert '<span class="painel-mes-atual">Outubro de 2026</span>' in html
    assert 'href="/admin/painel?mes=2026-11" class="btn btn-outline-primary btn-sm" rel="next">Novembro<span aria-hidden="true"> ›</span></a>' in html


def test_navegacao_vira_o_ano(admin, db):
    html = _html(admin.get("/admin/painel?mes=2026-01"))
    assert 'href="/admin/painel?mes=2025-12"' in html and "</span>Dezembro</a>" in html
    assert 'href="/admin/painel?mes=2026-02"' in html


def test_mes_seguinte_desabilitado_no_mes_atual(admin, db):
    html = _html(admin.get("/admin/painel"))
    assert '<span class="btn btn-outline-primary btn-sm disabled painel-mes-desabilitado" aria-disabled="true" title="Mês ainda não chegou">Dezembro<span aria-hidden="true"> ›</span></span>' in html
    assert "?mes=2026-12" not in html and 'rel="next"' not in html
    assert "inválido ou ainda não chegou" not in html


@pytest.mark.parametrize("mes", ["2026-12", "2027-03", "2026-13", "2026-1", "abc", "²²²²-11"])
def test_mes_futuro_ou_invalido_cai_no_atual_com_aviso(admin, db, mes):
    html = _html(admin.get("/admin/painel", params={"mes": mes}))
    assert "Indicadores de novembro de 2026." in html
    assert "O mês informado é inválido ou ainda não chegou. Exibindo novembro de 2026." in html
    assert "Mês em andamento, até 10/11" in html and 'aria-disabled="true"' in html


# --- Por especialidade ------------------------------------------------------------------------------

def test_clientes_distintos_com_plano_ativo_hoje(db, fono, psico):
    maria = criar_cliente(db, email="maria@exemplo.com")
    for nome in ("Filho 1", "Filho 2"):  # mesmo Cliente, dois atendidos com plano ativo: conta 1
        criar_plano(db, criar_paciente(db, maria.id, nome=nome), fono, [(datetime(2026, 11, 20, 9, 0), AGENDADA)])
    joao = criar_cliente(db, email="joao@exemplo.com")
    criar_plano(db, criar_paciente(db, joao.id), fono, [(datetime(2026, 11, 3, 9, 0), REALIZADA)])  # encerrado
    ana = criar_cliente(db, email="ana@exemplo.com")
    criar_plano(db, criar_paciente(db, ana.id), fono, [(datetime(2026, 11, 20, 10, 0), PENDENTE)],
                criado_em=datetime(2026, 11, 10, 12, 0))  # aguardando pagamento
    bia = criar_cliente(db, email="bia@exemplo.com")
    criar_plano(db, criar_paciente(db, bia.id), psico, [(datetime(2026, 11, 10, 9, 0), REALIZADA)])  # última hoje: ativo
    linhas = {l.nome: l.clientes_ativos for l in servico.montar_painel(db, NOVEMBRO, AGORA).especialidades}
    assert linhas == {"Fonoaudiologia": 1, "Psicologia": 1}


def test_tabela_por_especialidade_com_barras_e_nota(admin, db, fono, psico, paciente):
    criar_especialidade(db, "Terapia Ocupacional", "160.00")
    _contratar(db, paciente, fono, "300.00", EM_OUTUBRO_UTC)
    _contratar(db, paciente, fono, "300.00", EM_OUTUBRO_UTC)
    _contratar(db, paciente, psico, "150.00", EM_OUTUBRO_UTC)
    html = _html(admin.get("/admin/painel?mes=2026-10"))
    linhas = dict(re.findall(r'<tr class="(esp-[\w-]+)">(.*?)</tr>', html))
    assert set(linhas) == {"esp-fonoaudiologia", "esp-psicologia", "esp-neutra"}
    assert 'style="width: 100%"' in linhas["esp-fonoaudiologia"] and "R$ 600,00" in linhas["esp-fonoaudiologia"]
    assert linhas["esp-psicologia"].count('style="width: 50%"') == 1 and 'style="width: 25%"' in linhas["esp-psicologia"]
    assert linhas["esp-neutra"].count('style="width: 0%"') == 2
    assert "pessoas diferentes têm ao menos um plano ativo na especialidade hoje" in html
    assert html.count("<script") == 1  # só o bundle do Bootstrap


def test_especialidades_em_uma_consulta(db, fono, psico, paciente):
    _contratar(db, paciente, fono, "300.00", EM_OUTUBRO_UTC)
    comandos = []
    ouvinte = lambda *args: comandos.append(args[2])  # noqa: E731
    event.listen(db.get_bind(), "before_cursor_execute", ouvinte)
    try:
        servico._por_especialidade(db, OUTUBRO, AGORA)
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", ouvinte)
    assert len(comandos) == 1


# --- Ranking ----------------------------------------------------------------------------------------

def _linha(nome, planos, receita):
    return LinhaRanking(nome, "Fonoaudiologia", True, planos, Decimal(receita))


def test_posicoes_densas():
    linhas = [_linha("A", 3, "900"), _linha("B", 3, "500"), _linha("C", 2, "800"), _linha("D", 1, "100"), _linha("E", 1, "50")]
    assert [l.posicao for l in servico.posicoes_densas(linhas)] == [1, 1, 2, 3, 3]
    assert servico.posicoes_densas([]) == []


def test_ranking_com_empate_compartilha_a_posicao_e_ordena_por_receita(db, paciente):
    esp = criar_especialidade(db, "Fonoaudiologia")
    pessoas = {n: criar_funcionario(db, esp, email=f"{n.lower()}@exemplo.com", nome=n) for n in ("Ana", "Bia", "Caio", "Davi")}
    for nome, valores in {"Ana": ["100.00"], "Bia": ["900.00"], "Caio": ["300.00"], "Davi": ["50.00", "50.00"]}.items():
        for valor in valores:
            _contratar(db, paciente, pessoas[nome], valor, EM_OUTUBRO_UTC)
    ranking = servico.montar_painel(db, OUTUBRO, AGORA).ranking
    assert [(l.posicao, l.nome) for l in ranking] == [(1, "Davi"), (2, "Bia"), (2, "Caio"), (2, "Ana")]


def test_ranking_empate_total_desempata_pelo_nome(db, paciente):
    esp = criar_especialidade(db, "Fonoaudiologia")
    for nome in ("Zeca", "ana", "Bruno"):
        _contratar(db, paciente, criar_funcionario(db, esp, email=f"{nome.lower()}@exemplo.com", nome=nome), "100.00", EM_OUTUBRO_UTC)
    ranking = servico.montar_painel(db, OUTUBRO, AGORA).ranking
    assert [(l.posicao, l.nome) for l in ranking] == [(1, "ana"), (1, "Bruno"), (1, "Zeca")]


def test_ranking_com_mais_de_5_profissionais_mostra_5_linhas(admin, db, paciente):
    esp = criar_especialidade(db, "Fonoaudiologia")
    for i in range(8):
        prof = criar_funcionario(db, esp, email=f"p{i}@exemplo.com", nome=f"Prof {i}")
        for _ in range(1 + i % 3):
            _contratar(db, paciente, prof, f"{100 + i}.00", EM_OUTUBRO_UTC)
    ranking = servico.montar_painel(db, OUTUBRO, AGORA).ranking
    assert len(ranking) == 5
    assert [(l.posicao, l.nome, l.planos) for l in ranking] == [
        (1, "Prof 5", 3), (1, "Prof 2", 3), (2, "Prof 7", 2), (2, "Prof 4", 2), (2, "Prof 1", 2),
    ]
    html = _html(admin.get("/admin/painel?mes=2026-10"))
    lista = re.search(r'<ol class="painel-ranking-lista">.*?</ol>', html).group(0)
    assert lista.count("<li ") == 5
    assert re.findall(r'painel-posicao font-monospace-num">(\d+º)<', lista) == ["1º", "1º", "2º", "2º", "2º"]


# --- SQL ---------------------------------------------------------------------------------------------

def _contar_sql(client, db, url) -> int:
    comandos = []
    ouvinte = lambda *args: comandos.append(args[2])  # noqa: E731
    event.listen(db.get_bind(), "before_cursor_execute", ouvinte)
    try:
        assert client.get(url).status_code == 200
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", ouvinte)
    return len(comandos)


def test_painel_sem_n_mais_1_e_no_maximo_uma_consulta_a_mais(admin, db, fono, paciente):
    _contratar(db, paciente, fono, "300.00", EM_OUTUBRO_UTC)
    url = "/admin/painel?mes=2026-10"
    _contar_sql(admin, db, url)  # aquece: a 1ª requisição recarrega o usuário logado
    poucos = _contar_sql(admin, db, url)
    for i in range(10):
        prof = criar_funcionario(db, criar_especialidade(db, f"Especialidade {i:02d}", "100.00"), email=f"p{i}@exemplo.com", nome=f"P {i}")
        atendido = criar_paciente(db, criar_cliente(db, email=f"c{i}@exemplo.com").id)
        for confirmado in (EM_OUTUBRO_UTC, EM_SETEMBRO_UTC):
            _contratar(db, atendido, prof, "250.00", confirmado)
    db.expire_all()
    _contar_sql(admin, db, url)
    muitos = _contar_sql(admin, db, url)
    assert muitos == poucos <= SQL_ANTES + 1
    assert poucos == 8  # resumo (2) + mês anterior (1) + visão geral (3) + ranking (1) + especialidades (1)
