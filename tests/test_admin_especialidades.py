from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.models.especialidade import Especialidade
from app.models.plano import Plano
from app.models.sessao import StatusSessao
from app.schemas.especialidade import EspecialidadeCadastro, EspecialidadeValor
from app.services import especialidade_service
from tests.fabricas import (
    criar_admin,
    criar_cliente,
    criar_especialidade,
    criar_funcionario,
    criar_plano_com_sessao,
    logar,
)


@pytest.fixture
def admin_logado(client, db):
    logar(client, criar_admin(db).email)


# --- Schema --------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "entrada, esperado",
    [("150", "150.00"), ("150,5", "150.50"), ("1.250,00", "1250.00"), ("99.90", "99.90"), ("R$ 80,00", "80.00")],
)
def test_valor_da_sessao_aceita_formatos_brasileiros(entrada, esperado):
    assert EspecialidadeValor(valor_sessao=entrada).valor_sessao == Decimal(esperado)


@pytest.mark.parametrize(
    "entrada, mensagem",
    [("0", "maior que zero"), ("-10", "maior que zero"), ("abc", "valor numérico"), ("10,999", "duas casas"),
     ("100000000", "alto demais"), ("NaN", "maior que zero")],
)
def test_valor_da_sessao_invalido(entrada, mensagem):
    with pytest.raises(ValidationError, match=mensagem):
        EspecialidadeValor(valor_sessao=entrada)


# --- Service -------------------------------------------------------------------------------------

def test_cadastrar_e_listar_em_ordem_alfabetica(db):
    especialidade_service.cadastrar(db, EspecialidadeCadastro(nome="Psicologia", valor_sessao="180"))
    especialidade_service.cadastrar(db, EspecialidadeCadastro(nome="  Fonoaudiologia ", valor_sessao="150,00"))
    assert [(e.nome, e.valor_sessao) for e in especialidade_service.listar(db)] == [
        ("Fonoaudiologia", Decimal("150.00")),
        ("Psicologia", Decimal("180.00")),
    ]


def test_nome_repetido_e_recusado_sem_diferenciar_maiusculas(db):
    criar_especialidade(db, "Psicologia")
    with pytest.raises(especialidade_service.EspecialidadeJaCadastrada):
        especialidade_service.cadastrar(db, EspecialidadeCadastro(nome="PSICOLOGIA", valor_sessao="100"))


def test_atualizar_valor_nao_altera_planos_existentes(db):
    fono = criar_especialidade(db, "Fonoaudiologia", "150.00")
    funcionario = criar_funcionario(db, fono)
    plano = criar_plano_com_sessao(db, funcionario.usuario.id, fono, StatusSessao.AGENDADA, valor_total="1200.00")

    especialidade_service.atualizar_valor(db, fono.id, EspecialidadeValor(valor_sessao="200,00"))

    db.expire_all()
    assert db.get(Especialidade, fono.id).valor_sessao == Decimal("200.00")
    assert db.get(Plano, plano.id).valor_total == Decimal("1200.00")


def test_atualizar_especialidade_inexistente(db):
    with pytest.raises(especialidade_service.EspecialidadeNaoEncontrada):
        especialidade_service.atualizar_valor(db, 999, EspecialidadeValor(valor_sessao="100"))


# --- Rotas ---------------------------------------------------------------------------------------

def test_lista_vazia_orienta_a_cadastrar(client, admin_logado):
    html = client.get("/admin/especialidades").text
    assert "Nenhuma especialidade cadastrada" in html


def test_cadastrar_especialidade_pela_tela(client, db, admin_logado):
    resposta = client.post("/admin/especialidades/nova", data={"nome": "Psicopedagogia", "valor_sessao": "1.180,50"}, follow_redirects=False)
    assert resposta.status_code == 303
    html = client.get(resposta.headers["location"]).text
    assert "Especialidade cadastrada." in html
    assert "Psicopedagogia" in html and "R$ 1.180,50" in html


def test_especialidade_cadastrada_aparece_no_formulario_de_funcionario(client, db, admin_logado):
    client.post("/admin/especialidades/nova", data={"nome": "Psicologia", "valor_sessao": "180"})
    assert ">Psicologia</option>" in client.get("/admin/funcionarios/novo").text


@pytest.mark.parametrize(
    "dados, mensagem",
    [
        ({"nome": "", "valor_sessao": ""}, "Este campo é obrigatório."),
        ({"nome": "Psicologia", "valor_sessao": "zero"}, "Informe um valor numérico"),
        ({"nome": "fonoaudiologia", "valor_sessao": "100"}, "Já existe uma especialidade com este nome."),
    ],
)
def test_cadastro_de_especialidade_com_erro(client, db, admin_logado, dados, mensagem):
    criar_especialidade(db, "Fonoaudiologia")
    resposta = client.post("/admin/especialidades/nova", data=dados)
    assert resposta.status_code == 400
    assert mensagem in resposta.text
    assert db.query(Especialidade).count() == 1


def test_atualizar_valor_pela_tela(client, db, admin_logado):
    fono = criar_especialidade(db, "Fonoaudiologia", "150.00")
    url = f"/admin/especialidades/{fono.id}/editar"
    html = client.get(url).text
    assert 'value="150,00"' in html
    assert "Planos já existentes mantêm o valor" in html

    resposta = client.post(url, data={"valor_sessao": "175,00", "nome": "Ignorado"}, follow_redirects=False)
    assert resposta.status_code == 303
    db.refresh(fono)
    assert (fono.nome, fono.valor_sessao) == ("Fonoaudiologia", Decimal("175.00"))


def test_atualizar_valor_invalido_pela_tela(client, db, admin_logado):
    fono = criar_especialidade(db, "Fonoaudiologia", "150.00")
    resposta = client.post(f"/admin/especialidades/{fono.id}/editar", data={"valor_sessao": "-5"})
    assert resposta.status_code == 400
    assert "maior que zero" in resposta.text
    assert 'value="-5"' in resposta.text


def test_especialidade_inexistente_retorna_404(client, admin_logado):
    assert client.get("/admin/especialidades/999/editar").status_code == 404


@pytest.mark.parametrize("perfil", ["cliente", "funcionario"])
def test_rotas_de_especialidades_bloqueadas_para_quem_nao_e_admin(client, db, perfil):
    fono = criar_especialidade(db, "Fonoaudiologia", "150.00")
    usuario = criar_cliente(db) if perfil == "cliente" else criar_funcionario(db, fono).usuario
    logar(client, usuario.email)

    assert client.get("/admin/especialidades").status_code == 403
    assert client.post("/admin/especialidades/nova", data={"nome": "X", "valor_sessao": "1"}).status_code == 403
    assert client.post(f"/admin/especialidades/{fono.id}/editar", data={"valor_sessao": "1"}).status_code == 403
    db.refresh(fono)
    assert fono.valor_sessao == Decimal("150.00")


def test_placeholder_do_nome_e_renderizado_sem_aspas_escapadas(client, admin_logado):
    html = client.get("/admin/especialidades/nova").text
    assert 'placeholder="Ex.: Terapia Ocupacional"' in html
    assert "&#34;" not in html
