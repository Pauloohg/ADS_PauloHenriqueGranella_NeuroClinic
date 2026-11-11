import re
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import event, select

from app.models.avaliacao import Avaliacao
from app.models.cliente import Cliente
from app.models.funcionario import Funcionario
from app.models.pagamento import StatusPagamento
from app.models.plano import Plano
from app.models.sessao import Sessao, StatusSessao
from app.routers import agenda as agenda_router
from app.routers import meus_atendidos as meus_atendidos_router
from app.services import avaliacao_service, meus_atendidos_service, pagamento_service
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
AGORA_UTC = datetime(2026, 10, 28, 18, 0)  # o mesmo instante em UTC, sem fuso
CRIADO_HA_UMA_HORA = datetime(2026, 10, 28, 17, 0)  # UTC: plano pendente ainda dentro do prazo da RN09
PDF = b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\ntrailer << >>\n%%EOF\n"
OBSERVACAO = "<script>alert('x')</script> Trazer o caderno"

COM_AVALIACAO = datetime(2026, 10, 14, 9, 0)
NAO_COMPARECEU = datetime(2026, 10, 21, 9, 0)
AGUARDANDO = datetime(2026, 10, 27, 9, 0)
REMARCADA = datetime(2026, 11, 4, 9, 0)
FUTURA = datetime(2026, 11, 11, 9, 0)
DA_PSICOLOGA = datetime(2026, 10, 20, 10, 0)
PENDENTE_DO_FONO = datetime(2026, 11, 18, 10, 0)


@pytest.fixture(autouse=True)
def relogio_fixo(monkeypatch):
    monkeypatch.setattr(meus_atendidos_router, "agora_local", lambda: AGORA)
    monkeypatch.setattr(agenda_router, "agora_local", lambda: AGORA)
    monkeypatch.setattr(agenda_router, "agora_utc", lambda: AGORA_UTC)


@pytest.fixture(autouse=True)
def sem_email(monkeypatch):
    async def ignorar(*args, **kwargs):
        return True

    monkeypatch.setattr(avaliacao_service, "enviar_email", ignorar)


class _Fonte:
    def __init__(self, conteudo: bytes):
        self.conteudo, self.posicao = conteudo, 0

    def read(self, n: int) -> bytes:
        trecho = self.conteudo[self.posicao:self.posicao + n]
        self.posicao += len(trecho)
        return trecho


@pytest.fixture
def fono(db):
    return criar_funcionario(db, criar_especialidade(db, "Fonoaudiologia"))


@pytest.fixture
def psico(db):
    return criar_funcionario(db, criar_especialidade(db, "Psicologia"), email="psi@exemplo.com", nome="Paula Psi")


@pytest.fixture
def cliente(db):
    usuario = criar_cliente(db, nome="Maria Responsável")
    db.get(Cliente, usuario.id).cpf = "123.456.789-09"
    db.commit()
    return usuario


@pytest.fixture
def pedro(db, cliente):
    return criar_paciente(db, cliente.id, nome="Pedro Souza", nascimento=date(2018, 10, 29))


@pytest.fixture
def logado_fono(client, fono):
    logar(client, fono.usuario.email)
    return client


def _html(resposta) -> str:
    return " ".join(resposta.text.split())


def _texto(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def _sessao_id(db, quando: datetime) -> int:
    return db.scalar(select(Sessao.id).where(Sessao.data_hora == quando))


def _registrar_pdf(db, profissional, quando) -> int:
    sessao_id = _sessao_id(db, quando)
    avaliacao_service.registrar_comparecimento(
        db, profissional.usuario.id, sessao_id, _Fonte(PDF), "a.pdf", AGORA, AGORA_UTC, "http://x/",
    )
    return db.scalar(select(Avaliacao.id).where(Avaliacao.sessao_id == sessao_id))


# Pedro com o fono: avaliação, falta, aguardando registro, remarcada e futura; mais uma sessão da psicóloga e um
# plano do fono só aguardando pagamento (nenhum dos dois pode aparecer nas contagens do fono)
@pytest.fixture
def cenario(db, fono, psico, pedro):
    plano = criar_plano(db, pedro, fono, [
        (COM_AVALIACAO, AGENDADA), (NAO_COMPARECEU, AGENDADA), (AGUARDANDO, AGENDADA), (REMARCADA, AGENDADA),
        (FUTURA, AGENDADA),
    ], valor_total="987.65")
    plano.observacao = OBSERVACAO
    criar_plano(db, pedro, psico, [(DA_PSICOLOGA, AGENDADA)])
    criar_plano(db, pedro, fono, [(PENDENTE_DO_FONO, PENDENTE)], criado_em=CRIADO_HA_UMA_HORA)
    db.get(Sessao, _sessao_id(db, REMARCADA)).foi_remarcada = True
    db.commit()
    avaliacao_fono = _registrar_pdf(db, fono, COM_AVALIACAO)
    avaliacao_service.registrar_nao_comparecimento(db, fono.usuario.id, _sessao_id(db, NAO_COMPARECEU), AGORA, AGORA_UTC)
    avaliacao_psico = _registrar_pdf(db, psico, DA_PSICOLOGA)
    return {"plano": plano, "avaliacao_fono": avaliacao_fono, "avaliacao_psico": avaliacao_psico}


def _itens(html: str) -> list[str]:
    return re.findall(r'<li class="meus-consulta .*?</li>', html)


def _linhas(html: str) -> list[str]:
    return re.findall(r'<li class="meus-linha.*?</li>', html)


def _item_do_dia(html: str, quando: datetime) -> str:
    [item] = [i for i in _itens(html) if f'datetime="{quando.isoformat()}"' in i]
    return item


def _cartoes(html: str) -> list[str]:
    return re.findall(r'<article class="cartao meus-plano".*?</article>', html, re.S)


# --- Acesso -----------------------------------------------------------------------------------

@pytest.mark.parametrize("perfil", ["cliente", "admin"])
def test_cliente_e_admin_recebem_403(db, client, cliente, pedro, perfil):
    logar(client, cliente.email if perfil == "cliente" else criar_admin(db).email)
    assert client.get("/meus-atendidos").status_code == 403
    assert client.get(f"/meus-atendidos/{pedro.id}").status_code == 403


def test_visitante_vai_para_o_login(client, pedro):
    for url in ("/meus-atendidos", f"/meus-atendidos/{pedro.id}"):
        resposta = client.get(url, follow_redirects=False)
        assert resposta.status_code == 303 and resposta.headers["location"] == "/login"


@pytest.mark.parametrize("valor", ["abc", "-1", "1.5", "²", "9999999999999999999999", "9999"])
def test_id_invalido_ou_inexistente_da_404(logado_fono, cenario, valor):
    assert logado_fono.get(f"/meus-atendidos/{valor}").status_code == 404


def test_profissional_desativado_continua_vendo(db, logado_fono, fono, cenario, pedro):
    db.get(Funcionario, fono.usuario.id).ativo = False
    db.commit()
    assert logado_fono.get("/meus-atendidos").status_code == 200
    assert logado_fono.get(f"/meus-atendidos/{pedro.id}").status_code == 200


def test_navbar_marca_meus_atendidos(logado_fono):
    html = logado_fono.get("/meus-atendidos").text
    assert re.search(r'class="nav-link active" href="/meus-atendidos" aria-current="page"', html)


# --- Vínculo e isolamento entre Profissionais --------------------------------------------------

def test_atendido_de_outro_profissional_nao_aparece_e_da_404(db, logado_fono, fono, psico, cliente):
    ana = criar_paciente(db, cliente.id, nome="Ana Lima")
    criar_plano(db, ana, psico, [(datetime(2026, 10, 21, 9, 0), AGENDADA)])
    assert "Ana Lima" not in logado_fono.get("/meus-atendidos").text
    assert logado_fono.get(f"/meus-atendidos/{ana.id}").status_code == 404


def test_mesmo_atendido_com_dois_profissionais_mostra_so_o_proprio(db, client, fono, psico, cenario, pedro):
    logar(client, fono.usuario.email)
    html_fono = _html(client.get(f"/meus-atendidos/{pedro.id}"))
    assert len(_itens(html_fono)) == 5 and "20/10/2026" not in html_fono
    assert "Psicologia" not in html_fono and "Paula Psi" not in html_fono
    assert f"/avaliacoes/{cenario['avaliacao_fono']}/arquivo" in html_fono
    assert f"/avaliacoes/{cenario['avaliacao_psico']}/arquivo" not in html_fono
    assert html_fono.count('aria-label="Plano de ') == 1

    logar(client, psico.usuario.email)
    html_psico = _html(client.get(f"/meus-atendidos/{pedro.id}"))
    assert len(_itens(html_psico)) == 1 and "20/10/2026" in html_psico
    assert "Fonoaudiologia" not in html_psico and OBSERVACAO[:8] not in html_psico
    assert f"/avaliacoes/{cenario['avaliacao_psico']}/arquivo" in html_psico
    assert f"/avaliacoes/{cenario['avaliacao_fono']}/arquivo" not in html_psico
    lista_psico = _texto(_html(client.get("/meus-atendidos")))
    assert "Realizadas 1" in lista_psico and "Agendadas 0" in lista_psico


def test_so_pendente_nao_cria_vinculo_ate_ser_agendada(db, logado_fono, fono, cliente):
    bia = criar_paciente(db, cliente.id, nome="Bia Rocha")
    plano = criar_plano(db, bia, fono, [(datetime(2026, 11, 18, 10, 0), PENDENTE)], criado_em=CRIADO_HA_UMA_HORA)
    assert "Bia Rocha" not in logado_fono.get("/meus-atendidos").text
    assert logado_fono.get(f"/meus-atendidos/{bia.id}").status_code == 404
    for sessao in db.scalars(select(Sessao).where(Sessao.plano_id == plano.id)):
        sessao.status = AGENDADA
    db.commit()
    assert "Bia Rocha" in logado_fono.get("/meus-atendidos").text
    assert logado_fono.get(f"/meus-atendidos/{bia.id}").status_code == 200


def test_quem_teve_sessao_continua_na_lista(db, logado_fono, fono, cliente):
    caio = criar_paciente(db, cliente.id, nome="Caio Antigo")
    criar_plano(db, caio, fono, [(datetime(2026, 3, 2, 9, 0), REALIZADA)])
    html = _texto(_html(logado_fono.get("/meus-atendidos")))
    assert "Caio Antigo" in html and "Sem próxima consulta agendada." in html


# --- Contagens -------------------------------------------------------------------------------------

def test_lista_mostra_contagens_e_proxima_consulta(logado_fono, cenario, pedro):
    html = _html(logado_fono.get("/meus-atendidos"))
    [item] = _linhas(html)
    texto = _texto(item)
    assert "Pedro Souza" in texto and "8 anos" not in texto and "7 anos" in texto and "Fonoaudiologia" not in texto
    assert "Realizadas 2" in texto  # inclui o não comparecimento
    assert "Agendadas 2" in texto  # a remarcada e a futura; a encerrada conta como aguardando registro
    assert "Registro pendente 1 consulta aguardando registro" in texto
    assert "Próxima consulta qua, 04/11, 09:00" in texto
    assert f'href="/meus-atendidos/{pedro.id}"' in item


def test_lista_sem_aguardando_registro_nao_mostra_o_selo(db, logado_fono, fono, pedro):
    criar_plano(db, pedro, fono, [(FUTURA, AGENDADA)])
    assert "Aguardando registro" not in logado_fono.get("/meus-atendidos").text


def test_detalhe_mostra_resumo(logado_fono, cenario, pedro):
    html = _html(logado_fono.get(f"/meus-atendidos/{pedro.id}"))
    resumo = _texto(re.search(r'<ul class="meus-resumo.*?</ul>', html).group(0))
    for trecho in ("2 consultas realizadas", "2 agendadas à frente", "1 aguardando registro", "qua, 04/11, 09:00 próxima consulta"):
        assert trecho in resumo
    assert "Pedro Souza" in html and "7 anos. Dados das consultas com você." in html
    assert re.search(r'<a href="/meus-atendidos" class="pagina-voltar">.*?Meus atendidos</a>', html)


def test_service_do_detalhe_ignora_pendente_e_outro_profissional(db, fono, cenario, pedro):
    detalhe = meus_atendidos_service.detalhe_do_atendido(db, fono.usuario.id, pedro.id, AGORA)
    assert detalhe.resumo == meus_atendidos_service.ResumoAtendido(2, 2, 1, 1, 1)
    assert [p.id for p in detalhe.planos] == [cenario["plano"].id]
    assert {s.inicio for s in detalhe.sessoes} == {COM_AVALIACAO, NAO_COMPARECEU, AGUARDANDO, REMARCADA, FUTURA}


def test_cartao_do_plano(logado_fono, cenario, pedro):
    html = _html(logado_fono.get(f"/meus-atendidos/{pedro.id}"))
    [cartao] = [_texto(c) for c in _cartoes(html)]
    assert "Fonoaudiologia" not in cartao
    assert "Mensal, 2 vezes por semana, terças e quartas às 09:00" in cartao and "segunda" not in cartao
    assert "14/10/2026 a 11/11/2026" in cartao
    assert "2 de 5 consultas realizadas. 1 aguardando registro." in cartao
    assert 'aria-valuenow="2"' in html and 'aria-valuemax="5"' in html and "width: 40%" in html


def test_sessao_em_andamento_nao_e_aguardando_registro(db, logado_fono, fono, pedro):
    criar_plano(db, pedro, fono, [(datetime(2026, 10, 28, 14, 30), AGENDADA)])  # termina às 15:30
    html = _html(logado_fono.get(f"/meus-atendidos/{pedro.id}"))
    assert "Aguardando registro<" not in html and "/registrar" not in html
    assert "1 agendada" in _texto(html)


# --- Selos e ações ---------------------------------------------------------------------------------

def test_selos_e_acoes_de_cada_consulta(db, logado_fono, cenario, pedro):
    html = _html(logado_fono.get(f"/meus-atendidos/{pedro.id}"))
    com_avaliacao = _item_do_dia(html, COM_AVALIACAO)
    assert "Avaliação enviada" in com_avaliacao and "Realizada<" not in com_avaliacao  # um selo de estado só
    assert f'href="/avaliacoes/{cenario["avaliacao_fono"]}/arquivo" target="_blank" rel="noopener"' in com_avaliacao
    falta = _item_do_dia(html, NAO_COMPARECEU)
    assert "Não compareceu" in falta and "/avaliacoes/" not in falta
    aguardando = _item_do_dia(html, AGUARDANDO)
    assert "Aguardando registro" in aguardando
    assert f'href="/agenda/sessoes/{_sessao_id(db, AGUARDANDO)}/registrar"' in aguardando and "Registrar atendimento<" in aguardando
    remarcada = _item_do_dia(html, REMARCADA)
    assert "Agendada" in remarcada and "Remarcada" in remarcada and "/registrar" not in remarcada
    assert html.count("/registrar") == 1 and html.count("Ver avaliação") == 1


def test_ordem_das_consultas_da_mais_recente_para_a_mais_antiga(logado_fono, cenario, pedro):
    html = _html(logado_fono.get(f"/meus-atendidos/{pedro.id}"))
    anteriores = re.search(r'<section class="meus-grupo" id="anteriores".*?</section>', html).group(0)
    datas = [datetime.fromisoformat(d) for d in re.findall(r'<time class="meus-consulta-data" datetime="([^"]+)"', anteriores)]
    assert datas == sorted(datas, reverse=True) and datas == [NAO_COMPARECEU, COM_AVALIACAO]
    assert len(_itens(html)) == 5


def test_observacao_sai_escapada(logado_fono, cenario, pedro):
    html = logado_fono.get(f"/meus-atendidos/{pedro.id}").text
    escapada = "&lt;script&gt;alert(&#39;x&#39;)&lt;/script&gt; Trazer o caderno"
    assert "<script>alert(" not in html
    assert html.count(escapada) == 1  # uma vez só, no cartão do plano
    [cartao] = _cartoes(html)
    assert escapada in cartao and "<strong>Observação do plano:</strong>" in cartao


def test_observacao_fica_so_no_cartao_do_plano(logado_fono, cenario, pedro):
    html = _html(logado_fono.get(f"/meus-atendidos/{pedro.id}"))
    assert len(_itens(html)) == 5
    for item in _itens(html):
        assert "Trazer o caderno" not in item and "Observação" not in item


def test_cada_plano_mostra_a_propria_observacao_e_sem_observacao_nada_aparece(db, logado_fono, fono, pedro):
    com = criar_plano(db, pedro, fono, [(REMARCADA, AGENDADA)])
    com.observacao = "Usar letra bastão"
    criar_plano(db, pedro, fono, [(FUTURA, AGENDADA)])
    db.commit()
    html = _html(logado_fono.get(f"/meus-atendidos/{pedro.id}"))
    cartoes = _cartoes(html)
    assert len(cartoes) == 2
    assert sum("Usar letra bastão" in c for c in cartoes) == 1
    assert sum("Observação do plano:" in c for c in cartoes) == 1
    assert html.count("Usar letra bastão") == 1


# --- Privacidade (RNF09) ---------------------------------------------------------------------------

def test_lista_e_detalhe_nao_mostram_dados_pessoais_nem_valores(db, logado_fono, cliente, cenario, pedro, pasta_avaliacoes_temporaria):
    arquivo = db.get(Avaliacao, cenario["avaliacao_fono"]).arquivo_url
    proibidos = (
        "123.456.789-09", "12345678909", "99999-1234", cliente.email, "Maria Responsável", "Maria",
        "29/10/2018", "2018-10-29", "987,65", "987.65", "R$", "Pix", "Boleto", "desconto",
        arquivo, str(pasta_avaliacoes_temporaria), "dados/avaliacoes",
    )
    for url in ("/meus-atendidos", f"/meus-atendidos/{pedro.id}"):
        html = logado_fono.get(url).text
        for proibido in proibidos:
            assert proibido not in html, (url, proibido)
    # "pagamento" só pode aparecer na nota de rodapé do detalhe, que diz justamente que ele não aparece
    assert "pagamento" not in logado_fono.get("/meus-atendidos").text.lower()
    detalhe = _html(logado_fono.get(f"/meus-atendidos/{pedro.id}")).lower()
    nota = re.search(r'<p class="meus-nota">.*?</p>', detalhe).group(0)
    assert detalhe.count("pagamento") == 1 and "pagamento" in nota


# --- Lista: paginação, busca e estados vazios ----------------------------------------------------

@pytest.fixture
def vinte_e_um(db, fono, cliente):
    nomes = [f"Atendido {i:02d}" for i in range(21)]
    for nome in reversed(nomes):
        criar_plano(db, criar_paciente(db, cliente.id, nome=nome), fono, [(FUTURA, AGENDADA)])
    return nomes


def _nomes(html: str) -> list[str]:
    return re.findall(r'class="meus-link">([^<]+)</a>', html)


def test_lista_pagina_de_20_em_20_em_ordem_de_nome(logado_fono, vinte_e_um):
    primeira = _html(logado_fono.get("/meus-atendidos"))
    assert _nomes(primeira) == vinte_e_um[:20] and "Página 1 de 2" in primeira and "21 atendidos" in primeira
    segunda = _html(logado_fono.get("/meus-atendidos?pagina=2"))
    assert _nomes(segunda) == vinte_e_um[20:] and "Página 2 de 2" in segunda and "21 atendidos" in segunda


@pytest.mark.parametrize("valor, pagina_esperada", [("99", 2), ("0", 1), ("-1", 1), ("abc", 1)])
def test_lista_pagina_fora_do_intervalo(logado_fono, vinte_e_um, valor, pagina_esperada):
    resposta = logado_fono.get(f"/meus-atendidos?pagina={valor}")
    assert resposta.status_code == 200
    html = _html(resposta)
    assert f"Página {pagina_esperada} de 2" in html and "21 atendidos" in html
    assert len(_nomes(html)) == (1 if pagina_esperada == 2 else 20)


def test_busca_sem_diferenciar_maiusculas(db, logado_fono, fono, cliente, pedro):
    for nome in ("Ana Lima", "Mariana Souza"):
        criar_plano(db, criar_paciente(db, cliente.id, nome=nome), fono, [(FUTURA, AGENDADA)])
    criar_plano(db, pedro, fono, [(REMARCADA, AGENDADA)])
    html = _html(logado_fono.get("/meus-atendidos", params={"q": "ANA"}))
    assert _nomes(html) == ["Ana Lima", "Mariana Souza"] and 'value="ANA"' in html
    assert _nomes(_html(logado_fono.get("/meus-atendidos", params={"q": "  souza "}))) == ["Mariana Souza", "Pedro Souza"]


def test_busca_trata_curingas_como_texto(db, logado_fono, fono, pedro):
    criar_plano(db, pedro, fono, [(FUTURA, AGENDADA)])
    for termo in ("%", "_", "Pedro%"):
        assert _nomes(_html(logado_fono.get("/meus-atendidos", params={"q": termo}))) == []


def test_busca_sem_resultado(db, logado_fono, fono, pedro):
    criar_plano(db, pedro, fono, [(FUTURA, AGENDADA)])
    html = _html(logado_fono.get("/meus-atendidos", params={"q": "Zuleica"}))
    assert "Nenhum atendido com “Zuleica”." in html and ">Limpar busca</a>" in html
    assert "Você ainda não tem atendidos" not in html


def test_busca_paginada_mantem_o_termo(logado_fono, vinte_e_um):
    html = _html(logado_fono.get("/meus-atendidos", params={"q": "atendido"}))
    assert "/meus-atendidos?q=atendido&amp;pagina=2" in html


def test_lista_vazia(logado_fono):
    html = _html(logado_fono.get("/meus-atendidos"))
    assert "Você ainda não tem atendidos com consultas confirmadas." in html


# --- Detalhe: paginação ------------------------------------------------------------------------------

@pytest.fixture
def vinte_e_uma_sessoes(db, fono, pedro):
    criar_plano(db, pedro, fono, [(datetime(2026, 6, 1, 9, 0) + timedelta(days=i), REALIZADA) for i in range(21)])
    return pedro


@pytest.mark.parametrize("valor, pagina_esperada, itens", [("1", 1, 20), ("2", 2, 1), ("99", 2, 1), ("0", 1, 20), ("abc", 1, 20)])
def test_detalhe_pagina_de_20_em_20(logado_fono, vinte_e_uma_sessoes, valor, pagina_esperada, itens):
    resposta = logado_fono.get(f"/meus-atendidos/{vinte_e_uma_sessoes.id}?pagina={valor}")
    assert resposta.status_code == 200
    html = _html(resposta)
    assert f"Página {pagina_esperada} de 2" in html and len(_itens(html)) == itens
    assert "21 consultas realizadas" in _texto(html)
    if pagina_esperada == 2:
        [item] = _itens(html)
        assert "01/06/2026" in item


# --- Comandos SQL ------------------------------------------------------------------------------------

def _contar_comandos(db, funcao):
    comandos = []
    ouvinte = lambda *args: comandos.append(args[2])  # noqa: E731
    db.expire_all()
    event.listen(db.get_bind(), "before_cursor_execute", ouvinte)
    try:
        funcao()
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", ouvinte)
    return comandos


def _muitos_dados(db, fono, cliente, atendidos=25, sessoes=6):
    for i in range(atendidos):
        paciente = criar_paciente(db, cliente.id, nome=f"Muitos {i:02d}")
        for p in range(2):
            criar_plano(db, paciente, fono, [
                (datetime(2026, 9, 1, 8 + p, 0) + timedelta(days=7 * s), REALIZADA if s < 3 else AGENDADA)
                for s in range(sessoes)
            ])


@pytest.mark.parametrize("muitos", [False, True])
def test_lista_usa_uma_consulta_sql(db, fono, cliente, cenario, muitos):
    if muitos:
        _muitos_dados(db, fono, cliente)
    funcionario_id = fono.usuario.id
    resultado = {}
    comandos = _contar_comandos(db, lambda: resultado.update(
        pagina=meus_atendidos_service.atendidos_do_profissional(db, funcionario_id, AGORA)
    ))
    # Semana 8: as contagens dos chips do filtro valem para todos os atendidos, então são 2 comandos (nunca N+1)
    assert len(comandos) == 2
    assert len(resultado["pagina"].atendidos) == (20 if muitos else 1)
    assert all("GROUP BY" in comando for comando in comandos)


@pytest.mark.parametrize("muitos", [False, True])
def test_detalhe_usa_no_maximo_tres_consultas_sql(db, fono, cliente, pedro, cenario, muitos):
    if muitos:
        for p in range(6):
            criar_plano(db, pedro, fono, [(datetime(2026, 9, 1, 8 + p, 0) + timedelta(days=7 * s), REALIZADA) for s in range(8)])
    funcionario_id, paciente_id = fono.usuario.id, pedro.id
    resultado = {}
    comandos = _contar_comandos(db, lambda: resultado.update(
        detalhe=meus_atendidos_service.detalhe_do_atendido(db, funcionario_id, paciente_id, AGORA, 99)
    ))
    assert len(comandos) <= 3
    assert len(resultado["detalhe"].planos) == (7 if muitos else 1)


# --- Integração ---------------------------------------------------------------------------------------

def test_integracao_contratar_pagar_aparecer_e_registrar(db, client, fono, cliente, pedro):
    plano = criar_plano(db, pedro, fono, [(AGUARDANDO, PENDENTE), (FUTURA, PENDENTE)], criado_em=CRIADO_HA_UMA_HORA)
    pagamento = criar_pagamento(db, plano, status=StatusPagamento.PENDENTE)
    logar(client, fono.usuario.email)
    assert "Pedro Souza" not in client.get("/meus-atendidos").text

    resultado = pagamento_service.confirmar_pagamento(db, db.get(Plano, plano.id), pagamento, AGORA, AGORA_UTC)
    assert resultado.situacao == "confirmado"
    lista = _texto(_html(client.get("/meus-atendidos")))
    assert "Pedro Souza" in lista and "Realizadas 0" in lista and "Registro pendente 1 consulta aguardando registro" in lista
    detalhe = _html(client.get(f"/meus-atendidos/{pedro.id}"))
    assert "Aguardando registro" in _item_do_dia(detalhe, AGUARDANDO)

    resposta = client.post(
        f"/agenda/sessoes/{_sessao_id(db, AGUARDANDO)}/comparecimento",
        files={"arquivo": ("avaliacao.pdf", PDF, "application/pdf")}, follow_redirects=False,
    )
    assert resposta.status_code == 303
    lista = _texto(_html(client.get("/meus-atendidos")))
    assert "Realizadas 1" in lista and "aguardando registro" not in lista.lower() and "Registro pendente Nenhum" in lista
    detalhe = _html(client.get(f"/meus-atendidos/{pedro.id}"))
    item = _item_do_dia(detalhe, AGUARDANDO)
    assert "Avaliação enviada" in item and "Ver avaliação" in item and "Aguardando registro" not in item
    assert "1 consulta realizada" in _texto(detalhe) and 'id="aguardando-registro"' not in detalhe
