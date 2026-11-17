import re
from datetime import date, datetime

import pytest
from sqlalchemy import event

from app.models.cliente import Cliente
from app.models.sessao import StatusSessao
from app.routers import agenda as agenda_router
from app.routers import inicio as inicio_router
from app.routers import meus_atendidos as meus_atendidos_router
from app.services import meus_atendidos_service
from app.services.meus_atendidos_service import Responsavel
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

AGORA = datetime(2026, 10, 28, 9, 30)  # horário local, quarta-feira
HOJE_MAIS_TARDE = datetime(2026, 10, 28, 14, 0)
ONTEM = datetime(2026, 10, 27, 9, 0)  # agendada já encerrada: aparece em "A registrar" e no Início
SEMANA_PASSADA = datetime(2026, 10, 20, 9, 0)

NOME_RESPONSAVEL = "Joana Responsável"
TELEFONE = "(54) 99876-5432"
EMAIL = "responsavel@exemplo.com"
CPF = "529.982.247-25"
NASCIMENTO = date(2019, 3, 1)


@pytest.fixture(autouse=True)
def relogio_fixo(monkeypatch):
    for modulo in (meus_atendidos_router, agenda_router, inicio_router):
        monkeypatch.setattr(modulo, "agora_local", lambda: AGORA)


@pytest.fixture
def fono(db):
    return criar_funcionario(db, criar_especialidade(db, "Fonoaudiologia"))


@pytest.fixture
def psico(db):
    return criar_funcionario(db, criar_especialidade(db, "Psicologia"), email="psi@exemplo.com", nome="Paula Psi")


@pytest.fixture
def cliente(db):
    usuario = criar_cliente(db, email=EMAIL, nome=NOME_RESPONSAVEL)
    dados = db.get(Cliente, usuario.id)
    dados.cpf, dados.telefone = CPF, TELEFONE
    db.commit()
    return usuario


@pytest.fixture
def pedro(db, fono, cliente):
    paciente = criar_paciente(db, cliente.id, nome="Pedro Souza", nascimento=NASCIMENTO)
    criar_plano(db, paciente, fono, [(SEMANA_PASSADA, REALIZADA), (ONTEM, AGENDADA), (HOJE_MAIS_TARDE, AGENDADA)],
                valor_total="1234.56")
    return paciente


@pytest.fixture
def ana(db, psico, cliente):
    # Irmã do Pedro (mesmo Cliente), atendida só pela Psicologia
    paciente = criar_paciente(db, cliente.id, nome="Ana Irmã", nascimento=date(2016, 7, 9))
    criar_plano(db, paciente, psico, [(HOJE_MAIS_TARDE.replace(hour=15), AGENDADA)])
    return paciente


@pytest.fixture
def logado(client, fono):
    logar(client, fono.usuario.email)
    return client


def _bloco_responsavel(html: str) -> str | None:
    achado = re.search(r'<section aria-labelledby="titulo-responsavel">.*?</section>', html, re.S)
    return achado.group(0) if achado else None


def _texto(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def _mudar_telefone(db, cliente, telefone):
    db.get(Cliente, cliente.id).telefone = telefone
    db.commit()


# --- Aparece no detalhe ---------------------------------------------------------------------------

def test_detalhe_mostra_nome_e_telefone_do_responsavel(logado, pedro):
    resposta = logado.get(f"/meus-atendidos/{pedro.id}")
    assert resposta.status_code == 200
    bloco = _bloco_responsavel(resposta.text)
    assert bloco is not None
    assert '<h2 class="titulo-serifa meus-secao-titulo" id="titulo-responsavel">Responsável</h2>' in bloco
    assert NOME_RESPONSAVEL in bloco
    assert f'<a href="tel:54998765432">{TELEFONE}</a>' in bloco
    assert "Contato não informado" not in bloco


def test_bloco_do_responsavel_fica_entre_o_resumo_e_os_planos(logado, pedro):
    html = logado.get(f"/meus-atendidos/{pedro.id}").text
    assert html.index('class="meus-resumo"') < html.index('id="titulo-responsavel"') < html.index('id="titulo-planos"')


def test_detalhe_continua_sem_email_cpf_nascimento_e_valores(logado, pedro, ana):
    html = logado.get(f"/meus-atendidos/{pedro.id}").text
    assert EMAIL not in html
    assert CPF not in html
    assert "52998224725" not in html
    assert "01/03/2019" not in html
    assert "2019-03-01" not in html
    for proibido in ("1.234,56", "1234.56", "R$", "Pix", "Boleto", "desconto"):
        assert proibido not in html, proibido
    assert html.lower().count("pagamento") == 1  # só a nota do rodapé
    # Outros atendidos do mesmo Cliente nunca aparecem
    assert "Ana Irmã" not in html and "Ana" not in _texto(html)


@pytest.mark.parametrize("telefone", [None, "", "   "])
def test_telefone_vazio_mostra_contato_nao_informado(db, logado, cliente, pedro, telefone):
    _mudar_telefone(db, cliente, telefone)
    bloco = _bloco_responsavel(logado.get(f"/meus-atendidos/{pedro.id}").text)
    assert NOME_RESPONSAVEL in bloco
    assert "Contato não informado" in bloco
    assert "tel:" not in bloco


@pytest.mark.parametrize("telefone, digitos", [
    ("(54) 99876-5432", "54998765432"),
    ("+55 54 9.9876.5432", "5554998765432"),
    ('"><b>54</b>9', "549"),
    ("54 ９９８７６-٥٤٣٢ 1", "541"),  # dígitos de largura total e arábicos ficam de fora
    ("javascript:alert(1)//54", "154"),
])
def test_link_tel_so_com_digitos_ascii(db, logado, cliente, pedro, telefone, digitos):
    _mudar_telefone(db, cliente, telefone)
    bloco = _bloco_responsavel(logado.get(f"/meus-atendidos/{pedro.id}").text)
    assert re.findall(r'href="tel:([^"]*)"', bloco) == [digitos]
    assert "<b>" not in bloco  # o texto cru aparece escapado pelo autoescape


def test_telefone_sem_nenhum_digito_aparece_sem_link(db, logado, cliente, pedro):
    _mudar_telefone(db, cliente, "ligar à tarde")
    bloco = _bloco_responsavel(logado.get(f"/meus-atendidos/{pedro.id}").text)
    assert "ligar à tarde" in bloco
    assert "tel:" not in bloco and "Contato não informado" not in bloco


def test_responsavel_e_telefone_link_no_service(db, fono, pedro):
    detalhe = meus_atendidos_service.detalhe_do_atendido(db, fono.usuario.id, pedro.id, AGORA)
    assert detalhe.responsavel == Responsavel(NOME_RESPONSAVEL, TELEFONE)
    assert detalhe.responsavel.telefone_link == "54998765432"
    assert Responsavel("X", None).telefone_link == ""


# --- Não aparece em nenhuma outra tela -------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "/meus-atendidos",
    "/meus-atendidos?filtro=pendente",
    "/meus-atendidos?q=pedro",
    "/agenda",
    "/agenda?visao=dia&data=2026-10-28",
    "/agenda?visao=registrar",
    "/agenda?visao=periodo&inicio=2026-10-01&fim=2026-11-30",
    "/",
])
def test_responsavel_nao_aparece_fora_do_detalhe(logado, pedro, url):
    resposta = logado.get(url)
    assert resposta.status_code == 200
    assert "Pedro Souza" in resposta.text  # a tela mostra o atendido, mas não o responsável
    for proibido in (NOME_RESPONSAVEL, "Joana", "99876-5432", "54998765432", "tel:", EMAIL, CPF, "Contato não informado"):
        assert proibido not in resposta.text, (url, proibido)
    assert "titulo-responsavel" not in resposta.text


# --- Acesso: a regra não mudou ---------------------------------------------------------------------

def test_sem_vinculo_entre_profissionais_e_404(db, client, pedro, ana, psico):
    logar(client, psico.usuario.email)
    resposta = client.get(f"/meus-atendidos/{pedro.id}")
    assert resposta.status_code == 404
    assert NOME_RESPONSAVEL not in resposta.text and "99876-5432" not in resposta.text
    # A Psicologia vê o responsável no detalhe da Ana, sem o Pedro
    html = client.get(f"/meus-atendidos/{ana.id}").text
    assert NOME_RESPONSAVEL in _bloco_responsavel(html)
    assert "Pedro" not in html


def test_so_pendente_de_pagamento_nao_cria_vinculo(db, logado, fono, cliente):
    lia = criar_paciente(db, cliente.id, nome="Lia Pendente")
    criar_plano(db, lia, fono, [(HOJE_MAIS_TARDE, PENDENTE)])
    resposta = logado.get(f"/meus-atendidos/{lia.id}")
    assert resposta.status_code == 404
    assert NOME_RESPONSAVEL not in resposta.text


@pytest.mark.parametrize("valor", ["abc", "0", "-1", "²", "99999"])
def test_id_invalido_ou_inexistente_e_404_sem_responsavel(logado, pedro, valor):
    resposta = logado.get(f"/meus-atendidos/{valor}")
    assert resposta.status_code == 404
    assert NOME_RESPONSAVEL not in resposta.text


def test_cliente_e_admin_recebem_403_e_visitante_vai_ao_login(db, client, cliente, pedro):
    for email in (cliente.email, criar_admin(db).email):
        logar(client, email)
        resposta = client.get(f"/meus-atendidos/{pedro.id}")
        assert resposta.status_code == 403 and "99876-5432" not in resposta.text
    client.cookies.clear()
    resposta = client.get(f"/meus-atendidos/{pedro.id}", follow_redirects=False)
    assert resposta.status_code == 303 and resposta.headers["location"] == "/login"


# --- Comandos SQL ----------------------------------------------------------------------------------

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


@pytest.mark.parametrize("pagina", [1, 99])
def test_detalhe_com_responsavel_continua_com_no_maximo_tres_sql(db, fono, pedro, pagina):
    fid, pid = fono.usuario.id, pedro.id
    resultado = {}
    comandos = _contar(db, lambda: resultado.update(d=meus_atendidos_service.detalhe_do_atendido(db, fid, pid, AGORA, pagina)))
    assert len(comandos) <= 3
    assert resultado["d"].responsavel.nome == NOME_RESPONSAVEL
    # Nome e telefone vêm no comando do cabeçalho (JOIN); e-mail e CPF nunca são lidos
    assert sum("cliente.telefone" in c for c in comandos) == 1
    assert not any("email" in c or "cpf" in c for c in comandos)
