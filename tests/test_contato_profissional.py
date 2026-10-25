from datetime import datetime

import pytest
from pydantic import ValidationError

from app.models.cliente import Cliente
from app.models.funcionario import Funcionario
from app.models.sessao import StatusSessao
from app.routers import cliente as cliente_router
from app.routers import inicio as inicio_router
from app.schemas.funcionario import FuncionarioEdicao
from app.services import funcionario_service
from tests.fabricas import (
    criar_admin,
    criar_cliente,
    criar_especialidade,
    criar_funcionario,
    criar_paciente,
    criar_plano,
    logar,
)

AGORA = datetime(2026, 8, 1, 10, 0)  # sábado, horário local
EMAIL_LOGIN = "carla.login@exemplo.com"
CONTATO = {
    "telefone_contato": "  (54) 98888-7777 ",
    "email_contato": "  Carla.Contato@Clinica.com ",
    "formacao": "  Fonoaudiologia (UPF), especialização em linguagem infantil ",
    "registro_profissional": " CRFa 7-12345 ",
    "apresentacao": "  Atende crianças com atraso de fala e dificuldades de leitura. ",
}
FORM_NOVO = {"nome": "Carla Fono", "email": EMAIL_LOGIN, "senha": "provisoria1"}


@pytest.fixture(autouse=True)
def relogio_fixo(monkeypatch):
    for modulo in (cliente_router, inicio_router):
        monkeypatch.setattr(modulo, "agora_local", lambda: AGORA)


@pytest.fixture
def fono(db):
    return criar_especialidade(db, "Fonoaudiologia", "150.00")


@pytest.fixture
def carla(db, fono):
    return criar_funcionario(db, fono, email=EMAIL_LOGIN, nome="Carla Fono")


@pytest.fixture
def carla_completa(db, carla):
    funcionario = db.get(Funcionario, carla.usuario.id)
    for campo, valor in CONTATO.items():
        setattr(funcionario, campo, valor.strip().lower() if campo == "email_contato" else valor.strip())
    db.commit()
    return carla


@pytest.fixture
def admin_logado(client, db):
    logar(client, criar_admin(db).email)


@pytest.fixture
def maria_logada(client, db):
    maria = criar_cliente(db, email="maria@exemplo.com", nome="Maria Souza")
    db.get(Cliente, maria.id).cpf = "529.982.247-25"
    db.commit()
    logar(client, maria.email)
    return maria


def _html(resposta) -> str:
    return " ".join(resposta.text.split())


def _sem_email_de_login(html: str) -> None:
    assert EMAIL_LOGIN not in html and "carla.login" not in html


# --- Validação (schema) ---------------------------------------------------------------------------

def test_campos_vazios_ou_so_com_espacos_viram_none():
    dados = FuncionarioEdicao(nome="Carla", especialidade_id=1, **{campo: "   " for campo in CONTATO})
    assert all(getattr(dados, campo) is None for campo in CONTATO)


def test_campos_preenchidos_sao_limpos_e_email_vai_para_minusculas():
    dados = FuncionarioEdicao(nome="Carla", especialidade_id=1, **CONTATO)
    assert dados.telefone_contato == "(54) 98888-7777"
    assert dados.email_contato == "carla.contato@clinica.com"
    assert dados.registro_profissional == "CRFa 7-12345"
    assert dados.formacao.startswith("Fonoaudiologia") and not dados.apresentacao.endswith(" ")


@pytest.mark.parametrize("telefone", ["abc", "1234", "(54) 9999-ABCD", "1" * 21])
def test_telefone_de_contato_usa_a_regra_do_cliente(telefone):
    with pytest.raises(ValidationError, match="Informe um telefone válido, com DDD."):
        FuncionarioEdicao(nome="Carla", especialidade_id=1, telefone_contato=telefone)


@pytest.mark.parametrize("email", ["carla@", "carla", "carla @clinica.com", "@clinica.com"])
def test_email_de_contato_invalido(email):
    with pytest.raises(ValidationError, match="Informe um e-mail válido"):
        FuncionarioEdicao(nome="Carla", especialidade_id=1, email_contato=email)


@pytest.mark.parametrize("campo, limite", [
    ("email_contato", 120), ("formacao", 300), ("registro_profissional", 50), ("apresentacao", 500),
])
def test_limites_de_tamanho(campo, limite):
    valor = ("a" * (limite - 11) + "@exemplo.com") if campo == "email_contato" else "a" * (limite + 1)
    with pytest.raises(ValidationError, match="string_too_long|at most"):
        FuncionarioEdicao(nome="Carla", especialidade_id=1, **{campo: valor})


# --- RF11: Admin ----------------------------------------------------------------------------------

def test_formulario_do_admin_tem_os_cinco_campos_opcionais(client, fono, admin_logado):
    html = _html(client.get("/admin/funcionarios/novo"))
    for campo in CONTATO:
        assert f'name="{campo}"' in html, campo
    assert "Contato e formação" in html and html.count("(opcional)") >= 5


def test_cadastro_sem_os_campos_opcionais_grava_vazio(client, db, fono, admin_logado):
    resposta = client.post("/admin/funcionarios/novo", data={**FORM_NOVO, "especialidade_id": fono.id}, follow_redirects=False)
    assert resposta.status_code == 303
    funcionario = db.query(Funcionario).one()
    assert all(getattr(funcionario, campo) is None for campo in CONTATO)


def test_email_de_contato_nunca_e_copiado_do_email_de_login(client, db, fono, admin_logado):
    client.post("/admin/funcionarios/novo", data={**FORM_NOVO, "especialidade_id": fono.id, "email_contato": ""})
    assert db.query(Funcionario).one().email_contato is None


def test_cadastro_com_os_campos_preenchidos(client, db, fono, admin_logado):
    resposta = client.post("/admin/funcionarios/novo", data={**FORM_NOVO, "especialidade_id": fono.id, **CONTATO}, follow_redirects=False)
    assert resposta.status_code == 303
    funcionario = db.query(Funcionario).one()
    assert funcionario.telefone_contato == "(54) 98888-7777"
    assert funcionario.email_contato == "carla.contato@clinica.com"
    assert funcionario.registro_profissional == "CRFa 7-12345"
    assert funcionario.formacao == CONTATO["formacao"].strip()
    assert funcionario.apresentacao == CONTATO["apresentacao"].strip()


def test_edicao_preenche_altera_e_limpa_os_campos(client, db, carla_completa, admin_logado):
    url = f"/admin/funcionarios/{carla_completa.usuario.id}/editar"
    html = _html(client.get(url))
    assert 'value="CRFa 7-12345"' in html and "Atende crianças com atraso de fala" in html

    dados = {"nome": "Carla Fono", "especialidade_id": carla_completa.especialidade.id,
             **{campo: "" for campo in CONTATO}, "formacao": "Fonoaudiologia (PUCRS)"}
    assert client.post(url, data=dados, follow_redirects=False).status_code == 303
    funcionario = db.get(Funcionario, carla_completa.usuario.id)
    db.refresh(funcionario)
    assert funcionario.formacao == "Fonoaudiologia (PUCRS)"
    assert funcionario.telefone_contato is None and funcionario.email_contato is None


@pytest.mark.parametrize("campo, valor, mensagem", [
    ("telefone_contato", "telefone", "Informe um telefone válido, com DDD."),
    ("email_contato", "carla@", "Informe um e-mail válido, como nome@exemplo.com."),
    ("registro_profissional", "x" * 51, "Deve ter no máximo 50 caracteres."),
    ("apresentacao", "x" * 501, "Deve ter no máximo 500 caracteres."),
])
def test_erro_de_validacao_reexibe_o_formulario(client, db, fono, admin_logado, campo, valor, mensagem):
    dados = {**FORM_NOVO, "especialidade_id": fono.id, "formacao": "Fonoaudiologia", campo: valor}
    resposta = client.post("/admin/funcionarios/novo", data=dados)
    html = _html(resposta)
    assert resposta.status_code == 400
    assert mensagem in html
    assert 'value="Fonoaudiologia"' in html  # não perde o que já foi digitado
    assert db.query(Funcionario).count() == 0


def test_erro_na_edicao_reexibe_o_formulario(client, carla, admin_logado):
    url = f"/admin/funcionarios/{carla.usuario.id}/editar"
    dados = {"nome": "Carla Fono", "especialidade_id": carla.especialidade.id, "email_contato": "sem-arroba"}
    resposta = client.post(url, data=dados)
    assert resposta.status_code == 400 and "Informe um e-mail válido" in resposta.text


# --- RF04: Cliente ---------------------------------------------------------------------------------

def test_perfil_publico_nao_carrega_o_email_de_login(db, carla_completa, fono):
    [perfil] = funcionario_service.perfis_ativos_da_especialidade(db, fono.id)
    assert not hasattr(perfil, "email") and EMAIL_LOGIN not in repr(perfil)
    assert perfil.email_contato == "carla.contato@clinica.com"


def test_card_mostra_os_campos_preenchidos(client, fono, carla_completa, maria_logada):
    html = _html(client.get(f"/especialidades/{fono.id}/profissionais"))
    for trecho in (
        "Carla Fono", "CRFa 7-12345", "Fonoaudiologia (UPF), especialização em linguagem infantil",
        "Atende crianças com atraso de fala e dificuldades de leitura.",
        'href="tel:(54)98888-7777"', "(54) 98888-7777", 'href="mailto:carla.contato@clinica.com"',
    ):
        assert trecho in html, trecho
    assert f'href="/contratar/{carla_completa.usuario.id}"' in html
    _sem_email_de_login(html)


def test_card_sem_campos_mostra_so_o_nome(client, fono, carla, maria_logada):
    html = _html(client.get(f"/especialidades/{fono.id}/profissionais"))
    assert "Carla Fono" in html and f'href="/contratar/{carla.usuario.id}"' in html
    for classe in ("perfil-registro", "perfil-formacao", "perfil-apresentacao", "perfil-contato"):
        assert classe not in html.split("<main", 1)[1], classe
    assert "mailto:" not in html and "tel:" not in html
    _sem_email_de_login(html)


def test_card_mostra_so_o_que_foi_preenchido(client, db, fono, carla, maria_logada):
    funcionario = db.get(Funcionario, carla.usuario.id)
    funcionario.telefone_contato = "(54) 3333-4444"
    db.commit()
    html = _html(client.get(f"/especialidades/{fono.id}/profissionais"))
    assert "(54) 3333-4444" in html and "mailto:" not in html and "perfil-formacao" not in html


def test_contratacao_e_resumo_mostram_o_card_resumido_sem_email_de_login(client, db, fono, carla_completa, maria_logada):
    pedro = criar_paciente(db, maria_logada.id)
    tela = _html(client.get(f"/contratar/{carla_completa.usuario.id}"))
    resumido = tela[tela.index("cartao-profissional-resumido"):tela.index('id="form-contratacao"')]
    for trecho in ("Carla Fono", "CRFa 7-12345", "Fonoaudiologia (UPF)", "(54) 98888-7777", "carla.contato@clinica.com"):
        assert trecho in resumido, trecho
    _sem_email_de_login(tela)

    dados = {"paciente_id": str(pedro.id), "celula": ["0-14"], "frequencia_semanal": "1", "duracao": "mensal", "observacao": ""}
    _sem_email_de_login(_html(client.post(f"/contratar/{carla_completa.usuario.id}", data=dados)))


def test_contato_nao_aparece_fora_das_telas_do_rf04_e_rf11(client, db, carla_completa, maria_logada):
    pedro = criar_paciente(db, maria_logada.id)
    plano = criar_plano(db, pedro, carla_completa, [(datetime(2026, 8, 10, 14, 0), StatusSessao.AGENDADA)])
    for url in ("/", f"/planos/{plano.id}"):
        html = _html(client.get(url))
        assert "Carla Fono" in html, url
        for valor in ("98888-7777", "carla.contato@clinica.com", "CRFa 7-12345", "linguagem infantil"):
            assert valor not in html, (url, valor)
        _sem_email_de_login(html)
