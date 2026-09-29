from datetime import datetime, timezone

from fastapi import Depends

from app.main import app as fastapi_app
from app.models.usuario import TipoUsuario, Usuario
from app.routers.auth import COOKIE_TOKEN, MSG_CREDENCIAIS_INVALIDAS, get_usuario_por_tipo
from app.schemas.usuario import ClienteCadastro, SolicitacaoRedefinicaoSenha
from app.services import auth_service

FORM_CADASTRO = {"nome": "João Lima", "email": "joao@exemplo.com", "telefone": "54 98888-0000", "senha": "senha123"}


@fastapi_app.get("/_teste/somente-admin")
def _rota_somente_admin(usuario: Usuario = Depends(get_usuario_por_tipo(TipoUsuario.ADMIN))):
    return {"ok": True}


def _cadastrar(db):
    auth_service.cadastrar_cliente(db, ClienteCadastro(**FORM_CADASTRO), datetime.now(timezone.utc))


def test_paginas_de_formulario_carregam(client):
    for rota in ["/cadastro", "/login", "/esqueci-senha"]:
        assert client.get(rota).status_code == 200


def test_post_cadastro_valido_redireciona_para_login(client, db):
    resposta = client.post("/cadastro", data=FORM_CADASTRO, follow_redirects=False)
    assert resposta.status_code == 303
    assert resposta.headers["location"] == "/login?cadastro=ok"
    assert auth_service.buscar_por_email(db, "joao@exemplo.com") is not None


def test_post_cadastro_invalido_reexibe_formulario_com_erro(client):
    resposta = client.post("/cadastro", data={**FORM_CADASTRO, "senha": "123"})
    assert resposta.status_code == 400
    assert "pelo menos 8 caracteres" in resposta.text
    assert 'value="joao@exemplo.com"' in resposta.text  # não perde o que já foi digitado


def test_login_correto_grava_cookie_e_libera_acesso(client, db):
    _cadastrar(db)
    resposta = client.post("/login", data={"email": "joao@exemplo.com", "senha": "senha123"}, follow_redirects=False)
    assert resposta.status_code == 303
    assert COOKIE_TOKEN in resposta.cookies

    home = client.get("/")
    assert "João Lima" in home.text


def test_login_incorreto_mostra_mensagem_generica(client, db):
    _cadastrar(db)
    senha_errada = client.post("/login", data={"email": "joao@exemplo.com", "senha": "errada123"})
    email_inexistente = client.post("/login", data={"email": "x@exemplo.com", "senha": "senha123"})
    for resposta in (senha_errada, email_inexistente):
        assert resposta.status_code == 401
        assert MSG_CREDENCIAIS_INVALIDAS in resposta.text
    assert COOKIE_TOKEN not in client.cookies


def test_logout_remove_cookie(client, db):
    _cadastrar(db)
    client.post("/login", data={"email": "joao@exemplo.com", "senha": "senha123"})
    client.post("/logout")
    assert "João Lima" not in client.get("/").text


def test_esqueci_senha_responde_igual_para_email_existente_ou_nao(client, db):
    _cadastrar(db)
    existente = client.post("/esqueci-senha", data={"email": "joao@exemplo.com"})
    inexistente = client.post("/esqueci-senha", data={"email": "ninguem@exemplo.com"})
    assert existente.status_code == inexistente.status_code == 200
    assert existente.text == inexistente.text


def test_fluxo_de_redefinicao_pelo_link(client, db):
    _cadastrar(db)
    link = auth_service.solicitar_redefinicao_senha(
        db, SolicitacaoRedefinicaoSenha(email="joao@exemplo.com"), "http://testserver", datetime.now(timezone.utc)
    )
    caminho = link.removeprefix("http://testserver")
    assert client.get(caminho).status_code == 200

    resposta = client.post(caminho, data={"nova_senha": "novaSenha456"}, follow_redirects=False)
    assert resposta.headers["location"] == "/login?senha=redefinida"

    login = client.post("/login", data={"email": "joao@exemplo.com", "senha": "novaSenha456"}, follow_redirects=False)
    assert login.status_code == 303


def test_link_de_redefinicao_invalido_pede_nova_solicitacao(client):
    resposta = client.get("/redefinir-senha/token-qualquer")
    assert resposta.status_code == 400
    assert "Solicite um novo" in resposta.text


def test_rota_protegida_sem_login_redireciona_para_login(client):
    resposta = client.get("/_teste/somente-admin", follow_redirects=False)
    assert resposta.status_code == 303
    assert resposta.headers["location"] == "/login"


def test_rota_restrita_a_outro_perfil_retorna_403(client, db):
    _cadastrar(db)
    client.post("/login", data={"email": "joao@exemplo.com", "senha": "senha123"})
    assert client.get("/_teste/somente-admin").status_code == 403
