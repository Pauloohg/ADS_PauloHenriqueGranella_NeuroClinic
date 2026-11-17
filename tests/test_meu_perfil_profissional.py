import re
from datetime import datetime

import pytest
from sqlalchemy import func, select

from app.core.templating import AVISO_CONTATO_CLIENTE
from app.models.funcionario import Funcionario
from app.models.usuario import TipoUsuario, Usuario
from app.routers import cliente as cliente_router
from app.routers import inicio as inicio_router
from app.schemas.funcionario import CAMPOS_CONTATO
from tests.fabricas import criar_admin, criar_cliente, criar_especialidade, criar_funcionario, logar

AGORA = datetime(2026, 8, 3, 10, 0)  # segunda-feira, horário local
EMAIL_LOGIN = "carla.login@exemplo.com"
PREENCHIDO = {
    "telefone_contato": "(54) 98888-7777",
    "email_contato": "carla.contato@clinica.com",
    "formacao": "Fonoaudiologia (UPF), especialização em linguagem infantil",
    "registro_profissional": "CRFa 7-12345",
    "apresentacao": "Atende crianças com atraso de fala.",
}
VAZIO = {campo: "" for campo in CAMPOS_CONTATO}
XSS = "<script>alert(1)</script>"
XSS_ESCAPADO = "&lt;script&gt;alert(1)&lt;/script&gt;"


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
def bruno(db, fono):
    return criar_funcionario(db, fono, email="bruno@exemplo.com", nome="Bruno Fono")


@pytest.fixture
def carla_logada(client, carla):
    logar(client, EMAIL_LOGIN)
    return client


def _funcionario(db, resumo) -> Funcionario:
    db.expire_all()
    return db.get(Funcionario, resumo.usuario.id)


def _contato(db, resumo) -> dict:
    funcionario = _funcionario(db, resumo)
    return {campo: getattr(funcionario, campo) for campo in CAMPOS_CONTATO}


def _html(resposta) -> str:
    return " ".join(resposta.text.split())


# --- Acesso -------------------------------------------------------------------------------------------

def test_profissional_abre_o_proprio_perfil(carla_logada):
    resposta = carla_logada.get("/meu-perfil")
    assert resposta.status_code == 200
    html = _html(resposta)
    assert "Meu perfil" in html and 'action="/meu-perfil"' in html
    for campo in CAMPOS_CONTATO:
        assert f'name="{campo}"' in html, campo


@pytest.mark.parametrize("metodo", ["get", "post"])
def test_cliente_e_admin_recebem_403(db, client, metodo):
    for usuario in (criar_cliente(db), criar_admin(db)):
        logar(client, usuario.email)
        resposta = getattr(client, metodo)("/meu-perfil", **({"data": PREENCHIDO} if metodo == "post" else {}))
        assert resposta.status_code == 403


@pytest.mark.parametrize("metodo", ["get", "post"])
def test_visitante_vai_para_o_login(client, metodo):
    resposta = getattr(client, metodo)("/meu-perfil", follow_redirects=False,
                                       **({"data": PREENCHIDO} if metodo == "post" else {}))
    assert resposta.status_code == 303 and resposta.headers["location"] == "/login"


def test_navbar_do_profissional_na_ordem_pedida(carla_logada):
    menu = re.search(r'<ul class="navbar-nav menu-principal.*?</ul>', carla_logada.get("/").text, re.S).group(0)
    rotulos = [r.strip() for r in re.findall(r"</i>([^<]+)</a>", menu)]
    assert rotulos == ["Início", "Agenda", "Meus atendidos", "Meu perfil", "Alterar senha"]
    assert 'href="/meu-perfil"' in menu


def test_navbar_marca_meu_perfil_como_pagina_atual(carla_logada):
    html = carla_logada.get("/meu-perfil").text
    assert re.search(r'class="nav-link active" href="/meu-perfil" aria-current="page"', html)


def test_cliente_e_admin_nao_tem_meu_perfil_do_profissional_na_navbar(db, client):
    for usuario in (criar_cliente(db), criar_admin(db)):
        logar(client, usuario.email)
        assert 'href="/meu-perfil"' not in client.get("/").text


# --- Gravação ------------------------------------------------------------------------------------------

def test_grava_os_cinco_campos_e_mostra_sucesso(db, carla_logada, carla):
    resposta = carla_logada.post("/meu-perfil", data=PREENCHIDO, follow_redirects=False)
    assert resposta.status_code == 303 and resposta.headers["location"] == "/meu-perfil?ok=1"
    assert _contato(db, carla) == PREENCHIDO
    html = _html(carla_logada.get("/meu-perfil?ok=1"))
    assert '<div class="alert alert-success d-flex gap-2" role="status">' in html and "Dados salvos." in html
    for valor in PREENCHIDO.values():
        assert valor in html, valor


def test_sem_ok_nao_mostra_sucesso(carla_logada):
    assert "Dados salvos." not in carla_logada.get("/meu-perfil").text


def test_espacos_sao_limpos_e_email_vai_para_minusculas(db, carla_logada, carla):
    carla_logada.post("/meu-perfil", data={**PREENCHIDO, "email_contato": "  Carla.Contato@Clinica.COM ",
                                           "formacao": "  UPF  "})
    contato = _contato(db, carla)
    assert contato["email_contato"] == "carla.contato@clinica.com" and contato["formacao"] == "UPF"


@pytest.mark.parametrize("valor", ["", "   "])
def test_campo_vazio_vira_null(db, carla_logada, carla, valor):
    carla_logada.post("/meu-perfil", data=PREENCHIDO)
    resposta = carla_logada.post("/meu-perfil", data={campo: valor for campo in CAMPOS_CONTATO}, follow_redirects=False)
    assert resposta.status_code == 303
    assert all(v is None for v in _contato(db, carla).values())


def test_campos_ausentes_do_corpo_viram_null(db, carla_logada, carla):
    carla_logada.post("/meu-perfil", data=PREENCHIDO)
    assert carla_logada.post("/meu-perfil", data={"formacao": "UPF"}, follow_redirects=False).status_code == 303
    assert _contato(db, carla) == {**dict.fromkeys(CAMPOS_CONTATO), "formacao": "UPF"}


@pytest.mark.parametrize("campo, limite", [
    ("email_contato", 120), ("formacao", 300), ("registro_profissional", 50), ("apresentacao", 500),
])
def test_limites_de_tamanho(db, carla_logada, carla, campo, limite):
    no_limite = ("a" * (limite - 12) + "@exemplo.com") if campo == "email_contato" else "a" * limite
    assert carla_logada.post("/meu-perfil", data={**VAZIO, campo: no_limite}, follow_redirects=False).status_code == 303
    assert _contato(db, carla)[campo] == no_limite
    acima = ("a" * (limite - 11) + "@exemplo.com") if campo == "email_contato" else "a" * (limite + 1)
    resposta = carla_logada.post("/meu-perfil", data={**VAZIO, campo: acima})
    assert resposta.status_code == 400
    assert _contato(db, carla)[campo] == no_limite  # nada foi salvo


@pytest.mark.parametrize("email", ["carla@", "carla", "carla @clinica.com", "@clinica.com"])
def test_email_de_contato_invalido_400_com_formulario_preenchido(db, carla_logada, carla, email):
    resposta = carla_logada.post("/meu-perfil", data={**PREENCHIDO, "email_contato": email})
    assert resposta.status_code == 400
    html = _html(resposta)
    assert "Informe um e-mail válido, como nome@exemplo.com." in html
    assert 'id="email_contato"' in html and 'aria-invalid="true"' in html
    assert 'id="resumo-erros"' in html and "Nada foi salvo." in html
    for campo in ("formacao", "registro_profissional", "apresentacao", "telefone_contato"):
        assert PREENCHIDO[campo] in html, campo
    assert all(v is None for v in _contato(db, carla).values())


@pytest.mark.parametrize("telefone", ["abc", "1234", "1" * 21])
def test_telefone_invalido_usa_a_regra_do_cliente(db, carla_logada, carla, telefone):
    resposta = carla_logada.post("/meu-perfil", data={**VAZIO, "telefone_contato": telefone})
    assert resposta.status_code == 400 and "Informe um telefone válido, com DDD." in resposta.text
    assert _contato(db, carla)["telefone_contato"] is None


def test_duplo_envio_nao_causa_problema(db, carla_logada, carla):
    total_antes = db.scalar(select(func.count()).select_from(Funcionario))
    for _ in range(2):
        resposta = carla_logada.post("/meu-perfil", data=PREENCHIDO, follow_redirects=False)
        assert resposta.status_code == 303
    assert _contato(db, carla) == PREENCHIDO
    assert db.scalar(select(func.count()).select_from(Funcionario)) == total_antes


# --- Mass assignment e isolamento -----------------------------------------------------------------------

def test_campos_fora_do_perfil_no_corpo_sao_ignorados(db, carla_logada, carla, bruno):
    outra = criar_especialidade(db, "Psicologia", "200.00")
    usuario_antes = db.get(Usuario, carla.usuario.id)
    hash_antes = usuario_antes.senha_hash
    corpo = {
        **PREENCHIDO,
        "nome": "Nome Trocado", "email": "invasor@exemplo.com", "especialidade_id": str(outra.id),
        "ativo": "false", "senha": "outrasenha123", "nova_senha": "outrasenha123", "tipo": "admin",
        "usuario_id": str(bruno.usuario.id), "funcionario_id": str(bruno.usuario.id), "id": str(bruno.usuario.id),
    }
    assert carla_logada.post("/meu-perfil", data=corpo, follow_redirects=False).status_code == 303
    db.expire_all()
    usuario = db.get(Usuario, carla.usuario.id)
    funcionario = db.get(Funcionario, carla.usuario.id)
    assert usuario.nome == "Carla Fono" and usuario.email == EMAIL_LOGIN
    assert usuario.senha_hash == hash_antes and usuario.tipo == TipoUsuario.FUNCIONARIO
    assert funcionario.especialidade_id == carla.especialidade.id and funcionario.ativo is True
    assert _contato(db, carla) == PREENCHIDO
    assert all(v is None for v in _contato(db, bruno).values())  # o id no corpo não aponta para outro registro
    logar(carla_logada, EMAIL_LOGIN)  # a senha não mudou
    assert carla_logada.get("/meu-perfil").status_code == 200


def test_id_na_url_nao_existe(carla_logada, bruno):
    assert carla_logada.get(f"/meu-perfil/{bruno.usuario.id}").status_code == 404
    assert carla_logada.post(f"/meu-perfil/{bruno.usuario.id}", data=PREENCHIDO).status_code in (404, 405)


def test_um_profissional_nunca_altera_o_de_outro(db, client, carla, bruno):
    logar(client, "bruno@exemplo.com")
    client.post("/meu-perfil", data={**PREENCHIDO, "formacao": "Do Bruno"})
    logar(client, EMAIL_LOGIN)
    client.post("/meu-perfil", data=PREENCHIDO)
    assert _contato(db, bruno)["formacao"] == "Do Bruno"
    assert _contato(db, carla) == PREENCHIDO
    html = client.get("/meu-perfil").text
    assert "Do Bruno" not in html and "bruno@exemplo.com" not in html and "Bruno Fono" not in html


def test_profissional_desativado_consegue_editar(db, client, carla):
    _funcionario(db, carla).ativo = False
    db.commit()
    logar(client, EMAIL_LOGIN)
    assert client.get("/meu-perfil").status_code == 200
    assert client.post("/meu-perfil", data=PREENCHIDO, follow_redirects=False).status_code == 303
    funcionario = _funcionario(db, carla)
    assert funcionario.formacao == PREENCHIDO["formacao"] and funcionario.ativo is False


def test_tela_mostra_os_dados_de_acesso_so_para_leitura(carla_logada, carla):
    html = carla_logada.get("/meu-perfil").text
    assert "Carla Fono" in html and EMAIL_LOGIN in html and "Fonoaudiologia" in html
    for proibido in ('name="nome"', 'name="email"', 'name="especialidade_id"', 'name="ativo"', 'name="senha"'):
        assert proibido not in html, proibido
    assert 'href="/conta/alterar-senha"' in html


# --- Escape de HTML -------------------------------------------------------------------------------------

def _gravar_xss(db, carla_logada, carla):
    dados = {**VAZIO, "formacao": "F " + XSS, "registro_profissional": "R" + XSS[:40], "apresentacao": "A " + XSS}
    assert carla_logada.post("/meu-perfil", data=dados, follow_redirects=False).status_code == 303
    assert _contato(db, carla)["apresentacao"] == "A " + XSS  # grava o texto como veio; o escape é na exibição


def test_xss_escapado_na_propria_tela(db, carla_logada, carla):
    _gravar_xss(db, carla_logada, carla)
    html = carla_logada.get("/meu-perfil").text
    assert XSS not in html
    assert html.count(XSS_ESCAPADO) >= 4  # textareas de formação e apresentação + prévia "Como o cliente vê"


def test_xss_escapado_no_400_da_propria_tela(carla_logada):
    resposta = carla_logada.post("/meu-perfil", data={**VAZIO, "apresentacao": XSS, "email_contato": "invalido"})
    assert resposta.status_code == 400
    assert XSS not in resposta.text and XSS_ESCAPADO in resposta.text


def test_xss_escapado_no_card_do_rf04(db, client, carla_logada, carla, fono):
    _gravar_xss(db, carla_logada, carla)
    logar(client, criar_cliente(db, email="maria@exemplo.com").email)
    html = client.get(f"/especialidades/{fono.id}/profissionais").text
    assert XSS not in html and "<script>alert" not in html
    assert f'<p class="perfil-apresentacao">A {XSS_ESCAPADO}</p>' in html
    assert XSS_ESCAPADO in html.split('class="perfil-formacao"', 1)[1]
    assert EMAIL_LOGIN not in html


# --- RF04 depois da edição -----------------------------------------------------------------------------

def test_card_do_rf04_reflete_a_edicao_e_nunca_mostra_o_email_de_login(db, client, carla_logada, carla, fono):
    carla_logada.post("/meu-perfil", data={**VAZIO, "registro_profissional": "CRFa 7-12345",
                                           "telefone_contato": "(54) 98888-7777"})
    logar(client, criar_cliente(db, email="maria@exemplo.com").email)
    html = _html(client.get(f"/especialidades/{fono.id}/profissionais"))
    assert "CRFa 7-12345" in html and "(54) 98888-7777" in html
    for ausente in ("perfil-formacao", "perfil-apresentacao", "mailto:"):
        assert ausente not in html.split("<main", 1)[1], ausente
    assert EMAIL_LOGIN not in html and "carla.login" not in html


# --- Admin continua editando (vale a última gravação) ---------------------------------------------------

def test_admin_continua_editando_e_vale_a_ultima_gravacao(db, client, carla, fono):
    logar(client, EMAIL_LOGIN)
    client.post("/meu-perfil", data={**PREENCHIDO, "formacao": "Pela profissional"})
    logar(client, criar_admin(db).email)
    url = f"/admin/funcionarios/{carla.usuario.id}/editar"
    assert 'value="CRFa 7-12345"' in client.get(url).text  # o Admin vê o que a profissional gravou
    dados = {"nome": "Carla Fono", "especialidade_id": str(fono.id), **PREENCHIDO, "formacao": "Pelo Admin"}
    assert client.post(url, data=dados, follow_redirects=False).status_code == 303
    assert _contato(db, carla)["formacao"] == "Pelo Admin"
    logar(client, EMAIL_LOGIN)
    client.post("/meu-perfil", data={**PREENCHIDO, "formacao": "De novo pela profissional"})
    assert _contato(db, carla)["formacao"] == "De novo pela profissional"


# --- Aviso de transparência ao Cliente (LGPD) ----------------------------------------------------------

AVISO = ("Seu nome e telefone ficam visíveis para os profissionais que atendem a pessoa cadastrada, "
         "apenas para contato sobre os atendimentos.")


def test_constante_do_aviso_tem_o_texto_pedido():
    assert AVISO_CONTATO_CLIENTE == AVISO


def test_aviso_aparece_no_cadastro_junto_do_telefone(client):
    html = _html(client.get("/cadastro"))
    assert f'<div class="form-text">{AVISO}</div>' in html
    assert html.index('id="telefone"') < html.index(AVISO) < html.index('id="senha"')


def test_aviso_aparece_no_meu_perfil_do_cliente_junto_do_telefone(db, client):
    logar(client, criar_cliente(db).email)
    html = _html(client.get("/perfil"))
    assert f'<div id="telefone-ajuda" class="form-text">{AVISO}</div>' in html
    assert 'aria-describedby="telefone-ajuda"' in html


def test_aviso_continua_no_400_do_cadastro(client):
    resposta = client.post("/cadastro", data={"nome": "", "email": "x", "telefone": "1", "senha": "1"})
    assert resposta.status_code == 400 and AVISO in _html(resposta)


def test_aviso_nao_aparece_no_perfil_do_profissional(carla_logada):
    assert AVISO not in _html(carla_logada.get("/meu-perfil"))
