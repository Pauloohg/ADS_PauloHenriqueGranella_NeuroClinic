import re
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import event, select

from app.models.avaliacao import Avaliacao
from app.models.cliente import Cliente
from app.models.funcionario import Funcionario
from app.models.sessao import Sessao, StatusSessao
from app.routers import agenda as agenda_router
from app.routers import historico as historico_router
from app.services import avaliacao_service, consultas_service
from tests.fabricas import (
    criar_admin,
    criar_cliente,
    criar_especialidade,
    criar_funcionario,
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

REALIZADA_COM_AVALIACAO = datetime(2026, 10, 21, 9, 0)
NAO_COMPARECEU = datetime(2026, 10, 22, 9, 0)
AGUARDANDO_REGISTRO = datetime(2026, 10, 27, 9, 0)
EM_ANDAMENTO = datetime(2026, 10, 28, 14, 30)  # termina às 15:30, depois de AGORA
AGENDADA_FUTURA = datetime(2026, 11, 4, 9, 0)
PENDENTE_FUTURA = datetime(2026, 11, 10, 10, 0)


@pytest.fixture(autouse=True)
def relogio_fixo(monkeypatch):
    monkeypatch.setattr(historico_router, "agora_local", lambda: AGORA)
    monkeypatch.setattr(agenda_router, "agora_local", lambda: AGORA)
    monkeypatch.setattr(agenda_router, "agora_utc", lambda: AGORA_UTC.replace(tzinfo=timezone.utc))


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
    return criar_cliente(db, nome="Maria Responsável")


@pytest.fixture
def pedro(db, cliente):
    return criar_paciente(db, cliente.id, nome="Pedro Souza", nascimento=date(2018, 10, 29))


@pytest.fixture
def ana(db, cliente):
    return criar_paciente(db, cliente.id, nome="Ana Souza", nascimento=date(2016, 3, 12))


@pytest.fixture
def logado(client, cliente):
    logar(client, cliente.email)
    return client


def _html(resposta) -> str:
    return " ".join(resposta.text.split())


def _sessao_id(db, quando: datetime) -> int:
    return db.scalar(select(Sessao.id).where(Sessao.data_hora == quando))


def _registrar_pdf(db, profissional, quando):
    avaliacao_service.registrar_comparecimento(
        db, profissional.usuario.id, _sessao_id(db, quando), _Fonte(PDF), "a.pdf", AGORA, AGORA_UTC, "http://x/",
    )
    return db.scalar(select(Avaliacao).where(Avaliacao.sessao_id == _sessao_id(db, quando)))


def _registrar_falta(db, profissional, quando):
    avaliacao_service.registrar_nao_comparecimento(db, profissional.usuario.id, _sessao_id(db, quando), AGORA, AGORA_UTC)


# Pedro com um caso de cada: avaliação, falta, aguardando registro, em andamento, remarcada e pendente
@pytest.fixture
def cenario(db, fono, pedro):
    criar_plano(db, pedro, fono, [
        (REALIZADA_COM_AVALIACAO, AGENDADA), (NAO_COMPARECEU, AGENDADA), (AGUARDANDO_REGISTRO, AGENDADA),
        (EM_ANDAMENTO, AGENDADA), (AGENDADA_FUTURA, AGENDADA),
    ])
    criar_plano(db, pedro, fono, [(PENDENTE_FUTURA, PENDENTE)], criado_em=CRIADO_HA_UMA_HORA)
    db.get(Sessao, _sessao_id(db, AGENDADA_FUTURA)).foi_remarcada = True
    db.commit()
    avaliacao = _registrar_pdf(db, fono, REALIZADA_COM_AVALIACAO)
    _registrar_falta(db, fono, NAO_COMPARECEU)
    return avaliacao


def _itens(html: str) -> list[str]:
    return re.findall(r'<li class="consulta-item.*?</li>', html)


def _item_do_dia(html: str, quando: datetime) -> str:
    data = re.compile(rf'consulta-data">[^<]*<span class="font-monospace-num">{quando:%d/%m/%Y}<')
    [item] = [i for i in _itens(html) if data.search(i)]
    return item


# --- Acesso e escolha do atendido (B1 e B2) -----------------------------------------------------

@pytest.mark.parametrize("perfil", ["funcionario", "admin"])
def test_profissional_e_admin_nao_acessam(db, client, fono, perfil):
    email = fono.usuario.email if perfil == "funcionario" else criar_admin(db).email
    logar(client, email)
    assert client.get("/historico").status_code == 403


def test_visitante_vai_para_o_login(client):
    resposta = client.get("/historico", follow_redirects=False)
    assert resposta.status_code == 303 and resposta.headers["location"] == "/login"


def test_sem_parametro_usa_o_primeiro_atendido_por_nome(db, logado, fono, pedro, ana):
    criar_plano(db, pedro, fono, [(datetime(2026, 10, 21, 9, 0), AGENDADA)])
    criar_plano(db, ana, fono, [(datetime(2026, 10, 23, 9, 0), AGENDADA)])
    html = _html(logado.get("/historico"))
    assert "Histórico de Ana Souza" in html
    assert "23/10/2026" in html and "21/10/2026" not in html


def test_filtro_nunca_mistura_sessoes_de_dois_atendidos(db, logado, fono, psico, pedro, ana):
    criar_plano(db, pedro, fono, [(datetime(2026, 10, 21, 9, 0), AGENDADA), (datetime(2026, 10, 30, 9, 0), AGENDADA)])
    criar_plano(db, ana, psico, [(datetime(2026, 10, 23, 10, 0), AGENDADA)])
    html_pedro = _html(logado.get(f"/historico?atendido={pedro.id}"))
    html_ana = _html(logado.get(f"/historico?atendido={ana.id}"))
    assert len(_itens(html_pedro)) == 2 and "23/10/2026" not in html_pedro and "Paula Psi" not in html_pedro
    assert len(_itens(html_ana)) == 1 and "21/10/2026" not in html_ana and "Carlos Fono" not in html_ana
    pagina = consultas_service.historico_do_atendido(db, pedro.cliente_id, pedro.id, AGORA)
    assert {c.id for c in pagina.consultas} == set(
        db.scalars(select(Sessao.id).where(Sessao.data_hora.in_([datetime(2026, 10, 21, 9, 0), datetime(2026, 10, 30, 9, 0)])))
    )


def test_atendido_de_outro_cliente_e_inexistente_dao_404(db, logado, fono, pedro):
    outro = criar_cliente(db, email="outro@exemplo.com", nome="Outra Pessoa")
    alheio = criar_paciente(db, outro.id, nome="Atendido Alheio")
    criar_plano(db, alheio, fono, [(datetime(2026, 10, 21, 9, 0), AGENDADA)])
    for valor in (alheio.id, 9999, "abc", ""):
        resposta = logado.get("/historico", params={"atendido": valor})
        assert resposta.status_code == 404
        assert "Atendido Alheio" not in resposta.text


def test_historico_de_outro_cliente_nao_volta_linhas_mesmo_chamando_o_service(db, cliente, fono):
    outro = criar_cliente(db, email="outro@exemplo.com", nome="Outra Pessoa")
    alheio = criar_paciente(db, outro.id, nome="Atendido Alheio")
    criar_plano(db, alheio, fono, [(datetime(2026, 10, 21, 9, 0), AGENDADA)])
    assert consultas_service.historico_do_atendido(db, cliente.id, alheio.id, AGORA).consultas == []


def test_seletor_so_aparece_com_mais_de_um_atendido(db, logado, cliente, pedro):
    assert 'aria-label="Escolher o atendido"' not in _html(logado.get("/historico"))
    ana = criar_paciente(db, cliente.id, nome="Ana Souza")
    html = _html(logado.get(f"/historico?atendido={pedro.id}"))
    assert 'aria-label="Escolher o atendido"' in html
    assert f'href="/historico?atendido={ana.id}" class="btn btn-sm btn-outline-primary"' in html
    assert re.search(rf'href="/historico\?atendido={pedro.id}" class="btn btn-sm btn-primary"\s*aria-current="page"', html)
    assert "Todos" not in html


def test_lista_de_atendidos_tem_link_para_o_historico(db, logado, pedro, ana):
    html = _html(logado.get("/pacientes"))
    for atendido in (pedro, ana):
        assert f'href="/historico?atendido={atendido.id}"' in html
    assert "Histórico<span class=\"visually-hidden\"> de Pedro Souza</span>" in html


def test_navbar_marca_historico_como_pagina_atual(logado, pedro):
    html = logado.get("/historico").text
    assert re.search(r'class="nav-link active" href="/historico" aria-current="page"', html)


# --- Conteúdo da lista (B3) ---------------------------------------------------------------------

def test_tres_status_com_selo_correto(logado, cenario):
    html = _html(logado.get("/historico"))
    assert 'status-pendente">Aguardando pagamento' in _item_do_dia(html, PENDENTE_FUTURA)
    assert 'status-ativo">Agendada' in _item_do_dia(html, AGENDADA_FUTURA)
    assert 'status-inativo">Realizada' in _item_do_dia(html, REALIZADA_COM_AVALIACAO)
    assert 'status-inativo">Realizada' in _item_do_dia(html, NAO_COMPARECEU)


def test_selo_remarcada_so_na_sessao_remarcada(logado, cenario):
    html = _html(logado.get("/historico"))
    assert "Remarcada" in _item_do_dia(html, AGENDADA_FUTURA)
    assert html.count('status-remarcada">Remarcada') == 1


def test_mostra_profissional_e_especialidade(logado, cenario):
    item = _item_do_dia(_html(logado.get("/historico")), AGENDADA_FUTURA)
    assert "Carlos Fono" in item and 'chip-especialidade">Fonoaudiologia' in item


def test_link_da_avaliacao_so_para_realizada_com_arquivo(logado, cenario):
    html = _html(logado.get("/historico"))
    item = _item_do_dia(html, REALIZADA_COM_AVALIACAO)
    assert "Avaliação disponível" in item and "Liberada em" in item and "28/10/2026" in item
    assert f'href="/avaliacoes/{cenario.id}/arquivo" target="_blank" rel="noopener"' in item
    assert html.count("/avaliacoes/") == 1 and html.count("Ver avaliação") == 1


def test_data_de_liberacao_em_horario_local(db, logado, fono, pedro):
    criar_plano(db, pedro, fono, [(datetime(2026, 10, 21, 9, 0), AGENDADA)])
    avaliacao_service.registrar_comparecimento(  # 01:30 UTC de 29/10 = 22:30 de 28/10 em Brasília
        db, fono.usuario.id, _sessao_id(db, datetime(2026, 10, 21, 9, 0)), _Fonte(PDF), "a.pdf", AGORA,
        datetime(2026, 10, 29, 1, 30), "http://x/",
    )
    item = _item_do_dia(_html(logado.get("/historico")), datetime(2026, 10, 21, 9, 0))
    assert "Liberada em <span class=\"font-monospace-num\">28/10/2026</span>" in item


def test_nao_compareceu_sem_link(logado, cenario):
    item = _item_do_dia(_html(logado.get("/historico")), NAO_COMPARECEU)
    assert "Não compareceu" in item
    assert "/avaliacoes/" not in item and "Ver avaliação" not in item and "Avaliação disponível" not in item


def test_aguardando_registro_so_para_agendada_ja_encerrada(logado, cenario):
    html = _html(logado.get("/historico"))
    assert "Aguardando registro do profissional" in _item_do_dia(html, AGUARDANDO_REGISTRO)
    assert html.count("Aguardando registro do profissional") == 1
    for quando in (EM_ANDAMENTO, AGENDADA_FUTURA, PENDENTE_FUTURA):
        item = _item_do_dia(html, quando)
        assert "Aguardando registro" not in item and "Avaliação disponível" not in item and "Não compareceu" not in item


def test_ordem_da_mais_recente_para_a_mais_antiga(logado, cenario):
    html = _html(logado.get("/historico"))
    datas = [datetime.strptime(m, "%d/%m/%Y") for m in re.findall(r'consulta-data">[^<]*<span class="font-monospace-num">([\d/]+)', html)]
    assert datas == sorted(datas, reverse=True) and len(datas) == 6


def test_resumo_com_as_contagens(db, logado, cliente, pedro, cenario):
    html = _html(logado.get("/historico"))
    resumo = re.search(r'<ul class="historico-resumo.*?</ul>', html).group(0)
    textos = re.sub(r"<[^>]+>", "", resumo)
    assert "2 realizadas" in textos
    assert "3 agendadas" in textos  # aguardando registro, em andamento e a futura remarcada
    assert "1 aguardando pagamento" in textos
    assert "1 avaliação disponível" in textos
    pagina = consultas_service.historico_do_atendido(db, cliente.id, pedro.id, AGORA)
    assert pagina.resumo == consultas_service.ResumoHistorico(realizadas=2, agendadas=3, aguardando_pagamento=1, avaliacoes=1)


def test_paginacao_de_20_em_20(db, logado, fono, pedro):
    inicio = datetime(2026, 6, 1, 9, 0)
    criar_plano(db, pedro, fono, [(inicio + timedelta(days=i), REALIZADA) for i in range(21)])
    primeira = _html(logado.get("/historico"))
    assert len(_itens(primeira)) == 20 and "Página 1 de 2" in primeira
    assert f"/historico?atendido={pedro.id}&amp;pagina=2" in primeira
    assert "21/06/2026" in primeira and "01/06/2026" not in primeira
    segunda = _html(logado.get(f"/historico?atendido={pedro.id}&pagina=2"))
    assert len(_itens(segunda)) == 1 and "01/06/2026" in segunda and "Página 2 de 2" in segunda
    assert "21 realizadas" in re.sub(r"<[^>]+>", "", segunda)  # o resumo conta o atendido inteiro, não só a página
    alem = _html(logado.get(f"/historico?atendido={pedro.id}&pagina=9"))
    assert "Página 2 de 2" in alem and len(_itens(alem)) == 1


def test_pagina_invalida_volta_para_a_primeira(db, logado, fono, pedro):
    criar_plano(db, pedro, fono, [(datetime(2026, 10, 21, 9, 0), AGENDADA)])
    for valor in ("abc", "0", "-3"):
        assert len(_itens(_html(logado.get(f"/historico?atendido={pedro.id}&pagina={valor}")))) == 1


@pytest.mark.parametrize("valor, pagina_esperada, itens", [("99", 2, 1), ("0", 1, 20), ("-1", 1, 20), ("abc", 1, 20)])
def test_pagina_fora_do_intervalo_nunca_zera_o_resumo(db, logado, fono, pedro, valor, pagina_esperada, itens):
    criar_plano(db, pedro, fono, [(datetime(2026, 6, 1, 9, 0) + timedelta(days=i), REALIZADA) for i in range(21)])
    resposta = logado.get(f"/historico?atendido={pedro.id}&pagina={valor}")
    assert resposta.status_code == 200
    html = _html(resposta)
    texto = re.sub(r"<[^>]+>", "", html)
    assert f"Página {pagina_esperada} de 2" in html and len(_itens(html)) == itens
    assert "21 realizadas" in texto and "Ainda não há consultas" not in html


# --- Estados vazios ----------------------------------------------------------------------------

def test_cliente_sem_atendidos(logado):
    html = _html(logado.get("/historico"))
    assert "Nenhum atendido cadastrado" in html
    assert 'href="/pacientes/novo"' in html and "Cadastrar atendido" in html


def test_atendido_sem_consultas(logado, pedro):
    html = _html(logado.get("/historico"))
    assert "Ainda não há consultas para Pedro Souza" in html
    assert 'href="/especialidades" class="btn btn-primary">Contratar plano' in html
    assert "historico-resumo" not in html


# --- Privacidade (RNF09) ------------------------------------------------------------------------

def test_tela_nao_mostra_contato_cpf_nem_arquivo(db, logado, cliente, fono, cenario, pasta_avaliacoes_temporaria):
    perfil = db.get(Funcionario, fono.usuario.id)
    perfil.telefone_contato, perfil.email_contato = "(54) 98888-7777", "contato@clinica.exemplo"
    db.get(Cliente, cliente.id).cpf = "123.456.789-09"
    db.commit()
    html = logado.get("/historico").text
    for proibido in (
        fono.usuario.email, "contato@clinica.exemplo", "98888-7777", "123.456.789-09", "12345678909",
        cenario.arquivo_url, str(pasta_avaliacoes_temporaria), "dados/avaliacoes", ".pdf",
    ):
        assert proibido not in html
    assert "Carlos Fono" in html


# --- Camada de dados (B4) -----------------------------------------------------------------------

def test_historico_usa_uma_unica_consulta_sql(db, cliente, cenario, pedro):
    cliente_id, pedro_id = cliente.id, pedro.id
    db.expire_all()
    comandos = []
    ouvinte = lambda *args: comandos.append(args[2])  # noqa: E731
    event.listen(db.get_bind(), "before_cursor_execute", ouvinte)
    try:
        pagina = consultas_service.historico_do_atendido(db, cliente_id, pedro_id, AGORA)
        _ = [(c.profissional, c.especialidade, c.avaliacao_disponivel, c.liberado_em) for c in pagina.consultas]
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", ouvinte)
    assert len(pagina.consultas) == 6 and pagina.total == 6
    assert len(comandos) == 1
    sql = comandos[0]
    for trecho in ("JOIN plano", "JOIN paciente", "JOIN especialidade", "JOIN usuario", "LEFT OUTER JOIN avaliacao", "OVER"):
        assert trecho in sql
    assert "paciente.cliente_id" in sql and "paciente.id" in sql


def test_consultas_do_rf07_continuam_filtrando_por_aba(db, cliente, cenario):
    proximas = consultas_service.consultas_do_cliente(db, cliente.id, AGORA)
    anteriores = consultas_service.consultas_do_cliente(db, cliente.id, AGORA, consultas_service.ANTERIORES)
    assert all(c.data_hora >= AGORA for c in proximas.consultas) and proximas.total == 2
    assert all(c.data_hora < AGORA for c in anteriores.consultas) and anteriores.total == 4


# --- E-mail (B6) --------------------------------------------------------------------------------

def test_email_aponta_para_o_historico_do_atendido(db, fono, pedro, ana):
    criar_plano(db, ana, fono, [(datetime(2026, 10, 21, 9, 0), AGENDADA)])
    _, corpo = avaliacao_service.montar_email_nova_avaliacao(
        db, _sessao_id(db, datetime(2026, 10, 21, 9, 0)), "http://clinica.exemplo/",
    )
    assert f'href="http://clinica.exemplo/historico?atendido={ana.id}"' in corpo
    assert f"atendido={pedro.id}" not in corpo and "/consultas" not in corpo
    assert "Ver histórico</a>" in corpo and "Ver consultas" not in corpo


# --- Integração: Profissional registra, Cliente vê e abre ---------------------------------------

def test_integracao_registro_historico_e_download(db, client, cliente, fono, pedro):
    criar_plano(db, pedro, fono, [(datetime(2026, 10, 28, 14, 0), AGENDADA)])
    sessao_id = _sessao_id(db, datetime(2026, 10, 28, 14, 0))

    logar(client, fono.usuario.email)
    resposta = client.post(
        f"/agenda/sessoes/{sessao_id}/comparecimento",
        files={"arquivo": ("avaliacao.pdf", PDF, "application/pdf")}, follow_redirects=False,
    )
    assert resposta.status_code == 303
    avaliacao_id = db.scalar(select(Avaliacao.id).where(Avaliacao.sessao_id == sessao_id))

    logar(client, cliente.email)
    item = _item_do_dia(_html(client.get("/historico")), datetime(2026, 10, 28, 14, 0))
    assert "Avaliação disponível" in item and f'href="/avaliacoes/{avaliacao_id}/arquivo"' in item
    download = client.get(f"/avaliacoes/{avaliacao_id}/arquivo")
    assert download.status_code == 200 and download.content == PDF
    assert download.headers["content-disposition"].startswith("inline;")

    outro = criar_cliente(db, email="outro@exemplo.com", nome="Outra Pessoa")
    logar(client, outro.email)
    assert client.get(f"/avaliacoes/{avaliacao_id}/arquivo").status_code == 404
    assert client.get(f"/historico?atendido={pedro.id}").status_code == 404
