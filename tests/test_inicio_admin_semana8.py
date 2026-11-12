import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import event

from app.models.pagamento import StatusPagamento
from app.models.sessao import StatusSessao
from app.models.usuario import Usuario
from app.routers import admin as admin_router
from app.routers import inicio as inicio_router
from app.services import admin_clientes_service, funcionario_service, inicio_service, painel_service
from app.services.inicio_service import Cadastros, texto_clientes, texto_especialidades, texto_profissionais
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

AGORA = datetime(2026, 11, 10, 14, 30)  # horário local, terça-feira
NOVEMBRO = date(2026, 11, 1)
FUTURA = datetime(2026, 11, 18, 9, 0)
PASSADA = datetime(2026, 10, 6, 9, 0)


@pytest.fixture(autouse=True)
def relogio_fixo(monkeypatch):
    monkeypatch.setattr(inicio_router, "agora_local", lambda: AGORA)
    monkeypatch.setattr(admin_router, "agora_local", lambda: AGORA)


@pytest.fixture
def admin_logado(client, db):
    logar(client, criar_admin(db).email)
    return client


@pytest.fixture
def fono(db):
    return criar_funcionario(db, criar_especialidade(db, "Fonoaudiologia", "150.00"))


def _html(resposta) -> str:
    return " ".join(resposta.text.split())


def _corpo(html: str) -> str:
    return re.search(r"<main.*?</main>", html).group(0)


def _cliente(db, email, criado_em_utc):
    usuario = criar_cliente(db, email=email, nome=email.split("@")[0].capitalize())
    db.get(Usuario, usuario.id).criado_em = criado_em_utc
    db.commit()
    return usuario


def _contratar(db, paciente, funcionario, valor, confirmado_em, status=StatusPagamento.CONFIRMADO):
    plano = criar_plano(db, paciente, funcionario, [(FUTURA, AGENDADA)], valor_total=valor)
    criar_pagamento(db, plano, confirmado_em, status)
    return plano


# Novembro (local): dois pagamentos confirmados, um deles 00:30 do dia 1º; um de 31/10 23:30 (outubro em
# Brasília, novembro em UTC); um pendente. Dois clientes novos em novembro e um criado em 31/10 local
@pytest.fixture
def mes_com_dados(db, fono):
    ana = _cliente(db, "ana@exemplo.com", datetime(2026, 11, 3, 15, 0))
    _cliente(db, "caio@exemplo.com", datetime(2026, 11, 1, 3, 30))
    bia = _cliente(db, "bia@exemplo.com", datetime(2026, 11, 1, 2, 30))
    pedro = criar_paciente(db, ana.id, nome="Pedro Souza")
    lia = criar_paciente(db, bia.id, nome="Lia Prado")
    _contratar(db, pedro, fono, "1140.00", datetime(2026, 11, 5, 13, 0))
    _contratar(db, lia, fono, "855.45", datetime(2026, 11, 1, 3, 30))
    _contratar(db, lia, fono, "1200.00", datetime(2026, 11, 1, 2, 30))
    _contratar(db, pedro, fono, "999.00", None, StatusPagamento.PENDENTE)


# --- Faixa do mês: os mesmos números do Painel ---------------------------------------------------

def test_resumo_do_mes_e_o_mesmo_do_painel(db, mes_com_dados):
    resumo = painel_service.resumo_do_mes(db, NOVEMBRO)
    painel = painel_service.montar_painel(db, NOVEMBRO, AGORA)
    assert resumo == painel_service.ResumoDoMes(Decimal("1995.45"), 2, 2)
    assert (painel.receita, painel.planos_contratados, painel.novos_clientes) == (
        resumo.receita, resumo.planos_contratados, resumo.novos_clientes)


def test_faixa_da_home_mostra_o_que_o_painel_mostra_no_mes_atual(admin_logado, mes_com_dados):
    home = _html(admin_logado.get("/"))
    painel = _html(admin_logado.get("/admin/painel"))
    faixa = re.findall(r'<li><strong class="font-monospace-num">([^<]+)</strong>', home)
    cartoes = re.findall(r'<p class="painel-receita-valor font-monospace-num">([^<]+)</p>', painel)
    cartoes += re.findall(r'<div class="cartao painel-menor"> <strong class="font-monospace-num">([^<]+)</strong>', painel)
    assert faixa == cartoes == ["R$ 1.995,45", "2", "2"]
    assert "Novembro até hoje" in home
    assert _corpo(home).count("<strong") == 6  # 3 números da faixa e 3 títulos dos cadastros


def test_faixa_usa_o_mes_atual_do_painel(db, monkeypatch, admin_logado, mes_com_dados):
    monkeypatch.setattr(inicio_router, "agora_local", lambda: datetime(2026, 12, 1, 0, 30))
    html = _html(admin_logado.get("/"))
    assert "Dezembro até hoje" in html and "R$ 0,00" in html
    assert re.findall(r'<li><strong class="font-monospace-num">([^<]+)</strong>', html) == ["R$ 0,00", "0", "0"]


def test_faixa_textos_link_e_nota(admin_logado, mes_com_dados):
    texto = re.sub(r"<[^>]+>", " ", _html(admin_logado.get("/")))
    texto = " ".join(texto.split())
    assert "R$ 1.995,45 receita confirmada 2 planos contratados 2 clientes novos" in texto
    assert "Receita é o valor cobrado dos pagamentos confirmados no mês, já com desconto." in texto
    assert '<a href="/admin/painel" class="admin-inicio-link">Abrir painel gerencial</a>' in _html(admin_logado.get("/"))


def test_faixa_no_singular(db, admin_logado, fono):
    ana = _cliente(db, "ana@exemplo.com", datetime(2026, 11, 3, 15, 0))
    _contratar(db, criar_paciente(db, ana.id), fono, "150.00", datetime(2026, 11, 4, 13, 0))
    html = _html(admin_logado.get("/"))
    assert '<strong class="font-monospace-num">1</strong> <span>plano contratado</span>' in html
    assert '<strong class="font-monospace-num">1</strong> <span>cliente novo</span>' in html


def test_painel_continua_igual_depois_da_extracao(admin_logado, mes_com_dados):
    html = _html(admin_logado.get("/admin/painel"))
    assert "Indicadores de novembro de 2026" in html and "R$ 1.995,45" in html


@pytest.mark.parametrize("mes, titulo", [(date(2026, 1, 1), "Janeiro até hoje"), (date(2026, 3, 1), "Março até hoje")])
def test_titulo_do_mes(mes, titulo):
    assert inicio_service.titulo_do_mes(mes) == titulo


# --- Cadastros: textos -------------------------------------------------------------------------------

def _c(clientes=0, com_plano=0, ativos=0, desativados=0, especialidades=0, menor=None, maior=None):
    return Cadastros(clientes, com_plano, ativos, desativados, especialidades,
                     Decimal(menor) if menor else None, Decimal(maior) if maior else None)


@pytest.mark.parametrize("cadastros, esperado", [
    (_c(), "Nenhum cadastrado"),
    (_c(clientes=1), "1 cadastrado, 0 com plano ativo"),
    (_c(clientes=1, com_plano=1), "1 cadastrado, 1 com plano ativo"),
    (_c(clientes=5, com_plano=3), "5 cadastrados, 3 com plano ativo"),
])
def test_texto_clientes(cadastros, esperado):
    assert texto_clientes(cadastros) == esperado


@pytest.mark.parametrize("cadastros, esperado", [
    (_c(), "Nenhum cadastrado"),
    (_c(ativos=1, desativados=1), "1 ativo e 1 desativado"),
    (_c(ativos=4, desativados=1), "4 ativos e 1 desativado"),
    (_c(ativos=2), "2 ativos e 0 desativados"),
    (_c(desativados=2), "0 ativos e 2 desativados"),
])
def test_texto_profissionais(cadastros, esperado):
    assert texto_profissionais(cadastros) == esperado


@pytest.mark.parametrize("cadastros, esperado", [
    (_c(), "Nenhuma cadastrada"),
    (_c(especialidades=1, menor="150.00", maior="150.00"), "1 cadastrada, sessão de R$ 150,00"),
    (_c(especialidades=2, menor="150.00", maior="150.00"), "2 cadastradas, sessão de R$ 150,00"),
    (_c(especialidades=3, menor="150.00", maior="1200.50"), "3 cadastradas, sessões de R$ 150,00 a R$ 1.200,50"),
])
def test_texto_especialidades(cadastros, esperado):
    assert texto_especialidades(cadastros) == esperado


# --- Cadastros: dados e tela ------------------------------------------------------------------------------

def test_cadastros_contados_no_banco(db, fono):
    criar_funcionario(db, criar_especialidade(db, "Psicologia", "180.00"), email="psi@exemplo.com", nome="Paula Psi")
    terceiro = criar_funcionario(db, criar_especialidade(db, "Psicopedagogia", "200.00"), email="pp@exemplo.com", nome="Rui")
    funcionario_service.desativar(db, terceiro.usuario.id, AGORA)
    _cliente(db, "ana@exemplo.com", datetime(2026, 1, 1))
    assert inicio_service.resumo_dos_cadastros(db, AGORA) == Cadastros(
        1, 0, 2, 1, 3, Decimal("150.00"), Decimal("200.00"))


def test_com_plano_ativo_conta_clientes_distintos_e_so_plano_ativo(db, fono):
    dois_atendidos = _cliente(db, "ana@exemplo.com", datetime(2026, 1, 1))
    for nome in ("Pedro", "Lia"):
        criar_plano(db, criar_paciente(db, dois_atendidos.id, nome=nome), fono, [(FUTURA, AGENDADA)])
    so_encerrado = _cliente(db, "bia@exemplo.com", datetime(2026, 1, 1))
    criar_plano(db, criar_paciente(db, so_encerrado.id), fono, [(PASSADA, REALIZADA)])
    so_pendente = _cliente(db, "caio@exemplo.com", datetime(2026, 1, 1))
    criar_plano(db, criar_paciente(db, so_pendente.id), fono, [(FUTURA, PENDENTE)], criado_em=datetime(2026, 11, 10, 16, 0))
    _cliente(db, "davi@exemplo.com", datetime(2026, 1, 1))  # sem atendido
    ativo_e_encerrado = _cliente(db, "eva@exemplo.com", datetime(2026, 1, 1))
    paciente = criar_paciente(db, ativo_e_encerrado.id)
    criar_plano(db, paciente, fono, [(PASSADA, REALIZADA)])
    criar_plano(db, paciente, fono, [(datetime(2026, 11, 10, 9, 0), REALIZADA), (FUTURA, AGENDADA)])

    cadastros = inicio_service.resumo_dos_cadastros(db, AGORA)
    assert (cadastros.clientes, cadastros.clientes_com_plano_ativo) == (5, 2)
    lista = admin_clientes_service.listar(db, AGORA).clientes
    assert sum(1 for c in lista if c.planos_ativos) == cadastros.clientes_com_plano_ativo
    # um dia depois da última sessão, o plano está encerrado (regra derivada, com agora por parâmetro)
    depois = datetime(2026, 11, 19, 9, 0)
    assert inicio_service.resumo_dos_cadastros(db, depois).clientes_com_plano_ativo == 0


def test_lista_cadastros_com_links(db, admin_logado, fono):
    criar_especialidade(db, "Psicologia", "180.00")
    _cliente(db, "ana@exemplo.com", datetime(2026, 1, 1))
    html = _html(admin_logado.get("/"))
    itens = re.findall(r'<a href="([^"]+)" class="admin-inicio-item">.*?<strong>([^<]+)</strong> <span>([^<]+)</span>', html)
    assert itens == [
        ("/admin/clientes", "Clientes", "1 cadastrado, 0 com plano ativo"),
        ("/admin/funcionarios", "Profissionais", "1 ativo e 0 desativados"),
        ("/admin/especialidades", "Especialidades", "2 cadastradas, sessões de R$ 150,00 a R$ 180,00"),
    ]
    assert html.index('id="titulo-mes"') < html.index('id="titulo-cadastros"')  # Cadastros abaixo da faixa


def test_um_profissional_e_uma_especialidade(admin_logado, fono):
    html = _html(admin_logado.get("/"))
    assert "<strong>Profissionais</strong> <span>1 ativo e 0 desativados</span>" in html
    assert "<strong>Especialidades</strong> <span>1 cadastrada, sessão de R$ 150,00</span>" in html


# --- Corpo da página -----------------------------------------------------------------------------------------

def test_alterar_senha_saiu_do_corpo_e_continua_na_barra(admin_logado):
    html = _html(admin_logado.get("/"))
    assert "/conta/alterar-senha" not in _corpo(html) and "Altere a sua senha" not in html
    barra = re.search(r"<nav class=\"navbar.*?</nav>", html).group(0)
    assert 'href="/conta/alterar-senha"' in barra


def test_subtitulo_nao_fala_so_de_equipe_e_especialidades(admin_logado):
    html = _html(admin_logado.get("/"))
    assert "Como o mês está andando e os cadastros da clínica." in html
    assert "Gerencie a equipe e as especialidades da clínica." not in html


def test_sem_bloco_precisa_de_atencao_e_sem_javascript_novo(admin_logado, fono):
    html = _html(admin_logado.get("/"))
    assert "Precisa de atenção" not in html
    assert html.count("<script") == 1  # só o bundle do Bootstrap


def test_celular_empilha_em_uma_coluna():
    css = (Path(__file__).resolve().parent.parent / "app" / "static" / "css" / "styles.css").read_text(encoding="utf-8")
    bloco = css[css.index("/* --- Início do Administrador"):]
    movel = bloco[bloco.index("@media (max-width: 767.98px)"):]
    assert re.search(r"\.admin-inicio-numeros\s*\{\s*grid-template-columns: minmax\(0, 1fr\);", movel)


# --- Comandos SQL ----------------------------------------------------------------------------------------------

def _contar_sql(client, db) -> int:
    comandos = []
    ouvinte = lambda *args: comandos.append(args[2])  # noqa: E731
    event.listen(db.get_bind(), "before_cursor_execute", ouvinte)
    try:
        assert client.get("/").status_code == 200
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", ouvinte)
    return len(comandos)


def test_numero_de_consultas_nao_cresce_com_os_dados(db, admin_logado, fono, mes_com_dados):
    _contar_sql(admin_logado, db)  # aquece: a 1ª requisição recarrega o usuário logado
    poucos = _contar_sql(admin_logado, db)
    for i in range(15):
        especialidade = criar_especialidade(db, f"Especialidade {i}", f"{100 + i}.00")
        profissional = criar_funcionario(db, especialidade, email=f"p{i}@exemplo.com", nome=f"Prof {i}")
        cliente = _cliente(db, f"c{i}@exemplo.com", datetime(2026, 11, 2, 12, 0) + timedelta(hours=i))
        for j in range(2):
            _contratar(db, criar_paciente(db, cliente.id, nome=f"A{i}{j}"), profissional, "300.00",
                       datetime(2026, 11, 3, 12, 0) + timedelta(hours=i))
    db.expire_all()
    _contar_sql(admin_logado, db)
    assert _contar_sql(admin_logado, db) == poucos
    # faixa (contratos e clientes novos, reaproveitados do Painel) + cadastros (um comando agrupado)
    assert poucos == 3
