import re
from datetime import date, datetime, time, timedelta
from html.parser import HTMLParser
from pathlib import Path

import pytest
from sqlalchemy import event, select

from app.core.ids import ler_opcao
from app.core.templating import classe_especialidade
from app.models.avaliacao import Avaliacao
from app.models.cliente import Cliente
from app.models.funcionario import Funcionario
from app.models.sessao import Sessao, StatusSessao
from app.routers import meus_atendidos as meus_atendidos_router
from app.services import agenda_service, meus_atendidos_service
from app.services.meus_atendidos_service import (
    FILTRO_PENDENTE,
    FILTRO_TODOS,
    FILTROS,
    descrever_configuracao,
    quando_relativo,
)
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

AGORA = datetime(2026, 10, 28, 15, 0)  # horário local, quarta-feira

# Detalhe do Pedro: duas esperando registro, uma em andamento, duas próximas e duas anteriores
ESPERA_ANTIGA = datetime(2026, 10, 13, 9, 0)  # 15 dias: destacado (DIAS_ATRASO_DESTACADO = 14)
ESPERA_ONTEM = datetime(2026, 10, 27, 9, 0)
EM_ANDAMENTO = datetime(2026, 10, 28, 14, 30)  # termina às 15:30
AMANHA = datetime(2026, 10, 29, 10, 0)
REMARCADA = datetime(2026, 11, 3, 14, 0)
COM_AVALIACAO = datetime(2026, 10, 6, 9, 0)
FALTOU = datetime(2026, 10, 7, 9, 0)
OBSERVACAO = "Prefere atividades com jogos"
ARQUIVO = "a" * 32 + ".pdf"


@pytest.fixture(autouse=True)
def relogio_fixo(monkeypatch):
    monkeypatch.setattr(meus_atendidos_router, "agora_local", lambda: AGORA)


@pytest.fixture
def fono(db):
    return criar_funcionario(db, criar_especialidade(db, "Fonoaudiologia"))


@pytest.fixture
def cliente(db):
    usuario = criar_cliente(db, email="responsavel@exemplo.com", nome="Joana Responsável")
    dados = db.get(Cliente, usuario.id)
    dados.cpf, dados.telefone = "529.982.247-25", "(54) 99876-5432"
    db.commit()
    return usuario


@pytest.fixture
def logado(client, fono):
    logar(client, fono.usuario.email)
    return client


def _html(resposta) -> str:
    return " ".join(resposta.text.split())


def _texto(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def _linhas(html: str) -> list[str]:
    return re.findall(r'<li class="meus-linha.*?</li>', html)


def _linha(html: str, nome: str) -> str:
    [linha] = [l for l in _linhas(html) if f">{nome}</a>" in l]
    return linha


def _nomes(html: str) -> list[str]:
    return re.findall(r'class="meus-link">([^<]+)</a>', html)


def _grupo(html: str, id_: str) -> str | None:
    achado = re.search(rf'<section class="meus-grupo" id="{id_}".*?</section>', html)
    return achado.group(0) if achado else None


def _datas(trecho: str) -> list[datetime]:
    return [datetime.fromisoformat(d) for d in re.findall(r'class="meus-consulta-data" datetime="([^"]+)"', trecho)]


def _sessao(db, quando: datetime) -> Sessao:
    return db.scalars(select(Sessao).where(Sessao.data_hora == quando)).one()


def _avaliar(db, quando: datetime, comparecimento: bool) -> int:
    sessao = _sessao(db, quando)
    sessao.status = REALIZADA
    avaliacao = Avaliacao(sessao_id=sessao.id, arquivo_url=ARQUIVO if comparecimento else None,
                          comparecimento=comparecimento, liberado_em=datetime(2026, 10, 8, 12, 0))
    db.add(avaliacao)
    db.commit()
    return avaliacao.id


@pytest.fixture
def pedro(db, fono, cliente):
    paciente = criar_paciente(db, cliente.id, nome="Pedro Souza", nascimento=date(2019, 3, 1))
    plano = criar_plano(db, paciente, fono, [
        (COM_AVALIACAO, AGENDADA), (FALTOU, AGENDADA), (ESPERA_ANTIGA, AGENDADA), (ESPERA_ONTEM, AGENDADA),
        (EM_ANDAMENTO, AGENDADA), (AMANHA, AGENDADA), (REMARCADA, AGENDADA),
    ], valor_total="1234.56")
    plano.observacao = OBSERVACAO
    _sessao(db, REMARCADA).foi_remarcada = True
    db.commit()
    criar_pagamento(db, plano)
    paciente.avaliacao_id = _avaliar(db, COM_AVALIACAO, True)
    _avaliar(db, FALTOU, False)
    return paciente


# Lista: Ana (hoje, em dia), Bruno (amanhã, 2 pendentes), Caio (terça, 1 pendente), Davi (sem próxima)
@pytest.fixture
def quatro(db, fono, cliente):
    def novo(nome, sessoes):
        paciente = criar_paciente(db, cliente.id, nome=nome, nascimento=date(2016, 1, 10))
        criar_plano(db, paciente, fono, sessoes)
        return paciente

    return {
        "Ana Lima": novo("Ana Lima", [(datetime(2026, 10, 28, 16, 0), AGENDADA), (datetime(2026, 10, 21, 16, 0), REALIZADA)]),
        "Bruno Dias": novo("Bruno Dias", [
            (datetime(2026, 10, 20, 10, 0), AGENDADA), (datetime(2026, 10, 22, 10, 0), AGENDADA),
            (datetime(2026, 10, 29, 10, 0), AGENDADA),
        ]),
        "Caio Prado": novo("Caio Prado", [(datetime(2026, 10, 27, 14, 0), AGENDADA), (datetime(2026, 11, 3, 14, 0), AGENDADA)]),
        "Davi Rocha": novo("Davi Rocha", [(datetime(2026, 9, 1, 9, 0), REALIZADA)]),
    }


# --- Funções puras --------------------------------------------------------------------------------

@pytest.mark.parametrize("momento, esperado", [
    (datetime(2026, 10, 28, 16, 0), "hoje, 16:00"),
    (datetime(2026, 10, 28, 14, 30), "hoje, 14:30"),  # em andamento ainda é a próxima
    (datetime(2026, 10, 29, 10, 0), "amanhã, 10:00"),
    (datetime(2026, 11, 3, 14, 0), "ter, 03/11, 14:00"),
    (datetime(2026, 11, 17, 14, 0), "ter, 17/11, 14:00"),
    (datetime(2027, 1, 5, 9, 0), "ter, 05/01/2027, 09:00"),
])
def test_quando_relativo(momento, esperado):
    assert quando_relativo(momento, AGORA) == esperado


def test_quando_relativo_na_virada_do_mes_e_do_ano():
    assert quando_relativo(datetime(2026, 11, 1, 9, 0), datetime(2026, 10, 31, 23, 0)) == "amanhã, 09:00"
    assert quando_relativo(datetime(2027, 1, 1, 9, 0), datetime(2026, 12, 31, 8, 0)) == "amanhã, 09:00"
    assert quando_relativo(datetime(2027, 1, 4, 9, 0), datetime(2026, 12, 31, 8, 0)) == "seg, 04/01/2027, 09:00"


@pytest.mark.parametrize("duracao, frequencia, dias, esperado", [
    ("bimestral", 1, [2], "Bimestral, 1 vez por semana, quartas às 09:00"),
    ("mensal", 2, [2, 1], "Mensal, 2 vezes por semana, terças e quartas às 09:00"),
    ("anual", 3, [4, 0, 2], "Anual, 3 vezes por semana, segundas, quartas e sextas às 09:00"),
    ("quinzenal", 1, [], "Quinzenal, 1 vez por semana"),
])
def test_descrever_configuracao(duracao, frequencia, dias, esperado):
    assert descrever_configuracao(duracao, frequencia, dias, time(9)) == esperado


@pytest.mark.parametrize("valor, esperado", [
    ("pendente", FILTRO_PENDENTE), ("todos", FILTRO_TODOS), (None, FILTRO_TODOS), ("", FILTRO_TODOS),
    ("PENDENTE", FILTRO_TODOS), ("pendente ", FILTRO_TODOS), ("abc", FILTRO_TODOS), ("²", FILTRO_TODOS),
])
def test_filtro_lido_por_lista_fixa(valor, esperado):
    assert ler_opcao(valor, FILTROS, FILTRO_TODOS) == esperado


def test_reaproveita_as_regras_da_agenda():
    assert meus_atendidos_service.estado_da_consulta is agenda_service.estado_da_consulta
    assert meus_atendidos_service.ha_quanto_tempo is agenda_service.ha_quanto_tempo
    assert meus_atendidos_service.atraso_destacado is agenda_service.atraso_destacado


# --- Lista: filtro ----------------------------------------------------------------------------------

def test_filtro_pendente_mostra_so_quem_tem_registro_pendente(logado, quatro):
    html = _html(logado.get("/meus-atendidos?filtro=pendente"))
    assert _nomes(html) == ["Bruno Dias", "Caio Prado"]
    assert "2 atendidos" in _texto(html)
    assert re.search(r'aria-current="page">Com registro pendente <span class="font-monospace-num">2</span>', html)
    assert re.search(r'href="/meus-atendidos" class="btn btn-sm rounded-pill btn-outline-primary" >Todos <span class="font-monospace-num">4</span>', html)
    assert '<input type="hidden" name="filtro" value="pendente">' in html


def test_filtro_todos_e_o_padrao(logado, quatro):
    html = _html(logado.get("/meus-atendidos"))
    assert _nomes(html) == ["Ana Lima", "Bruno Dias", "Caio Prado", "Davi Rocha"]
    assert re.search(r'aria-current="page">Todos <span class="font-monospace-num">4</span>', html)
    assert 'href="/meus-atendidos?filtro=pendente"' in html
    assert 'name="filtro"' not in html


@pytest.mark.parametrize("valor", ["todos", "", "abc", "PENDENTE", "pendente%20", "²", "9" * 30])
def test_filtro_desconhecido_cai_em_todos(logado, quatro, valor):
    padrao = logado.get("/meus-atendidos").text
    resposta = logado.get(f"/meus-atendidos?filtro={valor}")
    assert resposta.status_code == 200 and resposta.text == padrao


def test_filtro_combinado_com_a_busca(db, logado, fono, cliente, quatro):
    criar_plano(db, criar_paciente(db, cliente.id, nome="Bruna Dias"), fono, [(datetime(2026, 11, 4, 9, 0), AGENDADA)])
    html = _html(logado.get("/meus-atendidos", params={"q": "dias", "filtro": "pendente"}))
    assert _nomes(html) == ["Bruno Dias"]
    assert 'value="dias"' in html
    # os chips contam todos os vinculados que combinam com a busca: Bruna e Bruno; só Bruno pendente
    assert re.search(r'>Todos <span class="font-monospace-num">2</span>', html)
    assert re.search(r'>Com registro pendente <span class="font-monospace-num">1</span>', html)
    assert 'href="/meus-atendidos?q=dias"' in html and 'href="/meus-atendidos?q=dias&amp;filtro=pendente"' in html


@pytest.fixture
def vinte_e_cinco(db, fono, cliente):
    # 25 atendidos; os 21 primeiros têm consulta esperando registro
    for i in range(25):
        sessoes = [(datetime(2026, 11, 4, 9, 0), AGENDADA)]
        if i < 21:
            sessoes.append((datetime(2026, 10, 20, 9, 0), AGENDADA))
        criar_plano(db, criar_paciente(db, cliente.id, nome=f"Atendido {i:02d}"), fono, sessoes)


def test_contagens_dos_chips_valem_para_todos_e_nao_so_para_a_pagina(logado, vinte_e_cinco):
    for url, quantos in (("/meus-atendidos", 20), ("/meus-atendidos?filtro=pendente", 20),
                         ("/meus-atendidos?filtro=pendente&pagina=2", 1), ("/meus-atendidos?pagina=2", 5)):
        html = _html(logado.get(url))
        assert len(_nomes(html)) == quantos, url
        assert re.search(r'>Todos <span class="font-monospace-num">25</span>', html), url
        assert re.search(r'>Com registro pendente <span class="font-monospace-num">21</span>', html), url


def test_paginacao_mantem_o_filtro_e_a_busca(logado, vinte_e_cinco):
    html = _html(logado.get("/meus-atendidos?filtro=pendente"))
    assert "Página 1 de 2" in html and 'href="/meus-atendidos?filtro=pendente&amp;pagina=2"' in html
    html = _html(logado.get("/meus-atendidos", params={"q": "atendido", "filtro": "pendente", "pagina": "2"}))
    assert "Página 2 de 2" in html and 'href="/meus-atendidos?q=atendido&amp;filtro=pendente&amp;pagina=1"' in html


@pytest.mark.parametrize("valor, pagina", [("99", 2), ("0", 1), ("abc", 1), ("2147483648", 1)])
def test_pagina_fora_do_intervalo_com_filtro(logado, vinte_e_cinco, valor, pagina):
    html = _html(logado.get(f"/meus-atendidos?filtro=pendente&pagina={valor}"))
    assert f"Página {pagina} de 2" in html and len(_nomes(html)) == (1 if pagina == 2 else 20)


def test_service_aceita_filtro_desconhecido(db, fono, quatro):
    resultado = meus_atendidos_service.atendidos_do_profissional(db, fono.usuario.id, AGORA, filtro="xyz")
    assert resultado.filtro == FILTRO_TODOS and resultado.total == 4
    assert (resultado.total_todos, resultado.total_pendentes) == (4, 2)


# --- Lista: colunas, selos e link --------------------------------------------------------------------

def test_cabecalho_das_colunas(logado, quatro):
    html = _html(logado.get("/meus-atendidos"))
    cabecalho = _texto(re.search(r'<div class="meus-linha meus-linha-cabecalho".*?</div>', html).group(0))
    assert cabecalho == "Atendido Realizadas Agendadas Registro pendente Próxima consulta"


def test_colunas_de_cada_linha(logado, quatro):
    html = _html(logado.get("/meus-atendidos"))
    assert _texto(_linha(html, "Ana Lima")) == (
        "Ana Lima 10 anos Realizadas 1 Agendadas 1 Registro pendente Nenhum Próxima consulta hoje, 16:00"
    )
    assert _texto(_linha(html, "Bruno Dias")) == (
        "Bruno Dias 10 anos Realizadas 0 Agendadas 1 Registro pendente 2 consultas aguardando registro de Bruno Dias: "
        "abrir a aba A registrar da agenda Próxima consulta amanhã, 10:00"
    )
    assert "Próxima consulta ter, 03/11, 14:00" in _texto(_linha(html, "Caio Prado"))
    davi = _linha(html, "Davi Rocha")
    assert '<span class="meus-nada" aria-hidden="true">—</span>' in davi
    assert "Sem próxima consulta agendada." in _texto(davi)


def test_selo_pendente_tem_texto_e_borda_ambar_so_com_pendencia(logado, quatro):
    html = _html(logado.get("/meus-atendidos"))
    for nome, pendente, texto in (("Bruno Dias", True, "2 consultas"), ("Caio Prado", True, "1 consulta"),
                                  ("Ana Lima", False, None), ("Davi Rocha", False, None)):
        linha = _linha(html, nome)
        assert ('class="meus-linha meus-linha-pendente"' in linha) is pendente
        assert ("status-pendente" in linha) is pendente
        if texto:
            assert re.search(rf'meus-selo-link">{texto}<span class="visually-hidden">', linha)
        else:
            assert "Nenhum" in linha


def test_especialidade_e_borda_da_especialidade_sairam_da_lista(logado, quatro):
    html = _html(logado.get("/meus-atendidos"))
    for linha in _linhas(html):
        assert "Fonoaudiologia" not in linha and "chip-especialidade" not in linha
        assert classe_especialidade("Fonoaudiologia") not in linha


class _Ancoras(HTMLParser):
    def __init__(self):
        super().__init__()
        self.aberta, self.aninhadas, self.links = None, 0, []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            if self.aberta is not None:
                self.aninhadas += 1
            self.aberta = dict(attrs)
            self.links.append(self.aberta)

    def handle_endtag(self, tag):
        if tag == "a":
            self.aberta = None


def test_selo_e_um_link_separado_do_link_esticado(logado, quatro):
    linha = _linha(_html(logado.get("/meus-atendidos")), "Bruno Dias")
    leitor = _Ancoras()
    leitor.feed(linha)
    assert leitor.aninhadas == 0
    hrefs = [(a.get("href"), a.get("class")) for a in leitor.links]
    assert hrefs[0] == (f"/meus-atendidos/{quatro['Bruno Dias'].id}", "meus-link")
    assert hrefs[1] == ("/agenda?visao=registrar", "badge rounded-pill status-pendente meus-selo-link")
    assert len(hrefs) == 2


def test_css_do_link_esticado_e_do_selo_acima_dele():
    css = (Path(__file__).resolve().parent.parent / "app" / "static" / "css" / "styles.css").read_text(encoding="utf-8")

    def regra(seletor):
        return re.search(rf"(?m)^{re.escape(seletor)}\s*\{{([^}}]*)\}}", css).group(1)

    assert "position: relative;" in regra(".meus-linha")
    assert "position: absolute;" in regra(".meus-link::after") and "inset: 0;" in regra(".meus-link::after")
    assert "position: relative;" in regra(".meus-selo-link") and "z-index: 1;" in regra(".meus-selo-link")
    assert "outline: 3px solid var(--bs-primary);" in regra(".meus-link:focus-visible::after")


# --- Lista: estados vazios -------------------------------------------------------------------------------

def test_busca_sem_resultado(logado, quatro):
    html = _html(logado.get("/meus-atendidos", params={"q": "xyz"}))
    assert "Nenhum atendido com “xyz”." in html
    assert '<a href="/meus-atendidos" class="btn btn-outline-primary btn-sm">Limpar busca</a>' in html
    assert 'role="search"' in html and "meus-filtros" not in html and _linhas(html) == []


def test_busca_sem_resultado_com_filtro_limpa_so_a_busca(logado, quatro):
    html = _html(logado.get("/meus-atendidos", params={"q": "xyz", "filtro": "pendente"}))
    assert '<a href="/meus-atendidos?filtro=pendente" class="btn btn-outline-primary btn-sm">Limpar busca</a>' in html


def test_lista_vazia_explica_quando_os_atendidos_aparecem(logado):
    html = _html(logado.get("/meus-atendidos"))
    assert "Você ainda não tem atendidos com consultas confirmadas." in html
    assert "depois que o pagamento de um plano com você é confirmado" in html
    assert '<a href="/agenda" class="btn btn-primary btn-sm">Ver agenda</a>' in html
    assert 'role="search"' not in html and "meus-filtros" not in html


def test_filtro_pendente_sem_nenhum_pendente(db, logado, fono, cliente):
    criar_plano(db, criar_paciente(db, cliente.id, nome="Ana Lima"), fono, [(datetime(2026, 11, 4, 9, 0), AGENDADA)])
    html = _html(logado.get("/meus-atendidos?filtro=pendente"))
    assert "Nenhum atendido com registro pendente." in html and ">Ver todos</a>" in html
    assert re.search(r'>Com registro pendente <span class="font-monospace-num">0</span>', html)
    assert _linhas(html) == []


# --- Detalhe: resumo, singular e plural ------------------------------------------------------------------

def _resumo(html: str) -> str:
    return _texto(re.search(r'<ul class="meus-resumo".*?</ul>', html).group(0))


def test_resumo_do_detalhe_com_pendencias(logado, pedro):
    html = _html(logado.get(f"/meus-atendidos/{pedro.id}"))
    assert _resumo(html) == (
        "2 consultas realizadas 3 agendadas à frente 2 aguardando registro. Ver abaixo hoje, 14:30 próxima consulta"
    )
    assert '<li class="meus-resumo-pendente">' in html and '<a href="#aguardando-registro">Ver abaixo</a>' in html
    assert 'id="aguardando-registro"' in html
    assert "Pedro Souza" in html and "7 anos. Dados das consultas com você." in html
    assert '<a href="/meus-atendidos" class="pagina-voltar">' in html


def test_resumo_sem_pendentes_nao_tem_ambar_nem_ancora(db, logado, fono, cliente):
    ana = criar_paciente(db, cliente.id, nome="Ana Lima")
    criar_plano(db, ana, fono, [(datetime(2026, 11, 4, 9, 0), AGENDADA)])
    html = _html(logado.get(f"/meus-atendidos/{ana.id}"))
    assert _resumo(html) == "0 consultas realizadas 1 agendada à frente 0 aguardando registro qua, 04/11, 09:00 próxima consulta"
    assert "meus-resumo-pendente" not in html and "#aguardando-registro" not in html and "Ver abaixo" not in html


@pytest.mark.parametrize("realizadas, futuras, esperado", [
    (1, 1, ["1 consulta realizada", "1 agendada à frente", "1 de 2 consultas realizadas."]),
    (2, 0, ["2 consultas realizadas", "0 agendadas à frente", "2 de 2 consultas realizadas."]),
    (0, 1, ["0 consultas realizadas", "1 agendada à frente", "0 de 1 consulta realizada."]),
    (1, 0, ["1 consulta realizada", "0 agendadas à frente", "1 de 1 consulta realizada."]),
])
def test_singular_e_plural(db, logado, fono, cliente, realizadas, futuras, esperado):
    bia = criar_paciente(db, cliente.id, nome="Bia Rocha")
    criar_plano(db, bia, fono, [(datetime(2026, 9, 1 + 7 * i, 9, 0), REALIZADA) for i in range(realizadas)]
                + [(datetime(2026, 11, 4 + 7 * i, 9, 0), AGENDADA) for i in range(futuras)])
    texto = _texto(_html(logado.get(f"/meus-atendidos/{bia.id}")))
    for trecho in esperado:
        assert trecho in texto
    if not futuras:
        assert "Sem próxima consulta" in texto


# --- Detalhe: planos ------------------------------------------------------------------------------------

def test_cartao_do_plano_com_progresso_e_pendentes(logado, pedro):
    html = _html(logado.get(f"/meus-atendidos/{pedro.id}"))
    [cartao] = re.findall(r'<article class="cartao meus-plano".*?</article>', html)
    texto = _texto(cartao)
    assert "Mensal, 3 vezes por semana, terças, quartas e quintas às 09:00" in texto
    assert "06/10/2026 a 03/11/2026" in texto
    assert "2 de 7 consultas realizadas. 2 aguardando registro." in texto
    assert f"Observação do plano: {OBSERVACAO}" in texto
    assert 'aria-valuenow="2"' in cartao and 'aria-valuemax="7"' in cartao and "width: 29%" in cartao


def test_observacao_aparece_uma_so_vez_mesmo_com_os_tres_grupos(logado, pedro):
    html = _html(logado.get(f"/meus-atendidos/{pedro.id}"))
    assert html.count(OBSERVACAO) == 1
    for id_ in ("aguardando-registro", "proximas", "anteriores"):
        assert OBSERVACAO not in _grupo(html, id_)


# --- Detalhe: os três grupos --------------------------------------------------------------------------------

def test_tres_grupos_na_ordem(logado, pedro):
    html = _html(logado.get(f"/meus-atendidos/{pedro.id}"))
    posicoes = [html.index(f'id="{id_}"') for id_ in ("aguardando-registro", "proximas", "anteriores")]
    assert posicoes == sorted(posicoes)
    assert "Aguardando registro, a mais antiga primeiro" in html
    assert _datas(_grupo(html, "aguardando-registro")) == [ESPERA_ANTIGA, ESPERA_ONTEM]
    assert _datas(_grupo(html, "proximas")) == [EM_ANDAMENTO, AMANHA, REMARCADA]
    assert _datas(_grupo(html, "anteriores")) == [FALTOU, COM_AVALIACAO]


def test_grupo_aguardando_registro(db, logado, pedro):
    grupo = _grupo(_html(logado.get(f"/meus-atendidos/{pedro.id}")), "aguardando-registro")
    itens = re.findall(r'<li class="meus-consulta .*?</li>', grupo)
    assert len(itens) == 2
    antiga, ontem = itens
    assert '<span class="agenda-atraso agenda-atraso-destacado">há 15 dias</span>' in antiga
    assert '<span class="agenda-atraso ">ontem</span>' in ontem
    for item, quando in ((antiga, ESPERA_ANTIGA), (ontem, ESPERA_ONTEM)):
        assert "agenda-estado-aguardando-registro" in item and ">Aguardando registro<" in item
        assert f'href="/agenda/sessoes/{_sessao(db, quando).id}/registrar"' in item and "Registrar atendimento<" in item


def test_grupo_proximas(logado, pedro):
    grupo = _grupo(_html(logado.get(f"/meus-atendidos/{pedro.id}")), "proximas")
    andamento, amanha, remarcada = re.findall(r'<li class="meus-consulta .*?</li>', grupo)
    assert "agenda-estado-em-andamento" in andamento and ">Em andamento<" in andamento
    assert "agenda-estado-agendada" in amanha and "agenda-selo-agendada" in amanha and "Remarcada" not in amanha
    assert ">Remarcada<" in remarcada and "status-remarcada" in remarcada
    assert "/registrar" not in grupo and "/avaliacoes/" not in grupo and "agenda-atraso" not in grupo


def test_grupo_anteriores(logado, pedro):
    grupo = _grupo(_html(logado.get(f"/meus-atendidos/{pedro.id}")), "anteriores")
    faltou, com_avaliacao = re.findall(r'<li class="meus-consulta .*?</li>', grupo)
    assert "agenda-estado-nao-compareceu" in faltou and ">Não compareceu<" in faltou and "<a " not in faltou
    assert "agenda-estado-avaliacao-enviada" in com_avaliacao and ">Avaliação enviada<" in com_avaliacao
    assert f'href="/avaliacoes/{pedro.avaliacao_id}/arquivo" target="_blank" rel="noopener"' in com_avaliacao


def test_grupo_vazio_nao_aparece(db, logado, fono, cliente):
    ana = criar_paciente(db, cliente.id, nome="Ana Lima")
    criar_plano(db, ana, fono, [(datetime(2026, 11, 4, 9, 0), AGENDADA)])
    html = _html(logado.get(f"/meus-atendidos/{ana.id}"))
    assert _grupo(html, "proximas") is not None
    assert _grupo(html, "aguardando-registro") is None and _grupo(html, "anteriores") is None
    assert "Anteriores" not in html and "a mais antiga primeiro" not in html


def test_estado_das_sessoes_vem_de_estado_da_consulta(db, fono, pedro):
    detalhe = meus_atendidos_service.detalhe_do_atendido(db, fono.usuario.id, pedro.id, AGORA)
    for sessao in detalhe.sessoes:
        assert sessao.estado == agenda_service.estado_da_consulta(sessao, AGORA)
    estados = {s.inicio: s.estado.chave for s in detalhe.sessoes}
    assert estados == {
        ESPERA_ANTIGA: "aguardando-registro", ESPERA_ONTEM: "aguardando-registro", EM_ANDAMENTO: "em-andamento",
        AMANHA: "agendada", REMARCADA: "agendada", COM_AVALIACAO: "avaliacao-enviada", FALTOU: "nao-compareceu",
    }


def test_paginacao_so_nas_anteriores_e_os_outros_grupos_continuam(db, logado, fono, pedro):
    criar_plano(db, pedro, fono, [(datetime(2026, 6, 1, 9, 0) + timedelta(days=i), REALIZADA) for i in range(20)])
    primeira = _html(logado.get(f"/meus-atendidos/{pedro.id}"))
    assert len(_datas(_grupo(primeira, "anteriores"))) == 20 and "Página 1 de 2" in primeira
    assert f'href="/meus-atendidos/{pedro.id}?pagina=2#anteriores"' in primeira
    segunda = _html(logado.get(f"/meus-atendidos/{pedro.id}?pagina=2"))
    assert _datas(_grupo(segunda, "anteriores")) == [datetime(2026, 6, 2, 9, 0), datetime(2026, 6, 1, 9, 0)]
    assert len(_datas(_grupo(segunda, "aguardando-registro"))) == 2 and len(_datas(_grupo(segunda, "proximas"))) == 3


def test_nota_de_privacidade_no_rodape(logado, pedro):
    html = _html(logado.get(f"/meus-atendidos/{pedro.id}"))
    assert ('<p class="meus-nota">Esta tela mostra só as consultas e os planos com você. '
            "Valores, pagamento e dados do responsável não aparecem.</p>") in html


# --- Acesso e 404 (como antes) --------------------------------------------------------------------------

@pytest.mark.parametrize("url", ["/meus-atendidos?filtro=pendente", "/meus-atendidos?q=a&filtro=todos"])
def test_cliente_e_admin_recebem_403_tambem_com_filtro(db, client, cliente, url):
    for email in (cliente.email, criar_admin(db).email):
        logar(client, email)
        assert client.get(url).status_code == 403


def test_visitante_com_filtro_vai_para_o_login(client):
    resposta = client.get("/meus-atendidos?filtro=pendente", follow_redirects=False)
    assert resposta.status_code == 303 and resposta.headers["location"] == "/login"


def test_atendido_de_outro_profissional_continua_404(db, client, fono, pedro):
    outro = criar_funcionario(db, fono.especialidade, email="outro@exemplo.com", nome="Outro Fono")
    logar(client, outro.usuario.email)
    assert client.get(f"/meus-atendidos/{pedro.id}").status_code == 404
    assert "Pedro Souza" not in client.get("/meus-atendidos?filtro=pendente").text


@pytest.mark.parametrize("valor", ["abc", "0", "-1", "²", "99999"])
def test_id_invalido_continua_404(logado, pedro, valor):
    assert logado.get(f"/meus-atendidos/{valor}").status_code == 404


def test_profissional_desativado_ve_a_lista_filtrada(db, logado, fono, pedro):
    db.get(Funcionario, fono.usuario.id).ativo = False
    db.commit()
    assert _nomes(_html(logado.get("/meus-atendidos?filtro=pendente"))) == ["Pedro Souza"]


# --- Privacidade (RNF09) ------------------------------------------------------------------------------------

def test_html_do_detalhe_nao_tem_dados_sensiveis(db, logado, cliente, pedro, pasta_avaliacoes_temporaria):
    html = logado.get(f"/meus-atendidos/{pedro.id}").text
    proibidos = {
        "CPF do responsável": "529.982.247-25",
        "CPF só dígitos": "52998224725",
        "telefone do responsável": "99876-5432",
        "e-mail do responsável": "responsavel@exemplo.com",
        "nome do responsável": "Joana",
        "data de nascimento (BR)": "01/03/2019",
        "data de nascimento (ISO)": "2019-03-01",
        "valor do plano (BR)": "1.234,56",
        "valor do plano": "1234.56",
        "moeda": "R$",
        "forma de pagamento": "Pix",
        "desconto": "desconto",
        "nome do arquivo da avaliação": ARQUIVO,
        "pasta das avaliações": str(pasta_avaliacoes_temporaria),
        "caminho padrão das avaliações": "dados/avaliacoes",
    }
    vazou = {rotulo: valor for rotulo, valor in proibidos.items() if valor in html}
    assert vazou == {}
    assert html.lower().count("pagamento") == 1  # só a nota do rodapé, que diz que ele não aparece


# --- Comandos SQL ---------------------------------------------------------------------------------------------

def _contar(db, funcao):
    comandos = []
    ouvinte = lambda *args: comandos.append(args[2])  # noqa: E731
    db.expire_all()
    event.listen(db.get_bind(), "before_cursor_execute", ouvinte)
    try:
        funcao()
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", ouvinte)
    return comandos


def _muitos(db, fono, cliente, quantos=30):
    for i in range(quantos):
        paciente = criar_paciente(db, cliente.id, nome=f"Muitos {i:02d}")
        for p in range(2):
            criar_plano(db, paciente, fono, [
                (datetime(2026, 9, 1, 8 + p, 0) + timedelta(days=7 * s), REALIZADA if s < 3 else AGENDADA)
                for s in range(10)
            ])


@pytest.mark.parametrize("muitos", [False, True])
@pytest.mark.parametrize("filtro, busca, pagina", [
    ("todos", "", 1), ("pendente", "", 1), ("pendente", "muitos", 2), ("todos", "", 99), ("pendente", "zzz", 1),
])
def test_lista_usa_no_maximo_duas_consultas_sql(db, fono, cliente, quatro, muitos, filtro, busca, pagina):
    if muitos:
        _muitos(db, fono, cliente)
    fid = fono.usuario.id
    comandos = _contar(db, lambda: meus_atendidos_service.atendidos_do_profissional(db, fid, AGORA, pagina, busca, filtro))
    assert 1 <= len(comandos) <= 2


@pytest.mark.parametrize("muitos", [False, True])
@pytest.mark.parametrize("pagina", [1, 2, 99])
def test_detalhe_usa_no_maximo_tres_consultas_sql_com_os_tres_grupos(db, fono, pedro, muitos, pagina):
    if muitos:
        for p in range(5):
            criar_plano(db, pedro, fono, [(datetime(2026, 5, 4, 8 + p, 0) + timedelta(days=7 * s), REALIZADA) for s in range(10)])
    fid, pid = fono.usuario.id, pedro.id
    resultado = {}
    comandos = _contar(db, lambda: resultado.update(d=meus_atendidos_service.detalhe_do_atendido(db, fid, pid, AGORA, pagina)))
    assert len(comandos) <= 3
    assert {s.estado.chave for s in resultado["d"].sessoes} >= {"aguardando-registro", "agendada"}
