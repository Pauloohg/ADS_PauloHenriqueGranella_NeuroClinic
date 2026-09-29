from datetime import timedelta

import pytest
from pydantic import ValidationError

from app.core import security
from app.core.config import settings
from app.models.cliente import Cliente
from app.models.usuario import TipoUsuario
from app.schemas.usuario import (
    ClienteCadastro,
    ConfirmacaoRedefinicaoSenha,
    Login,
    SolicitacaoRedefinicaoSenha,
)
from app.services import auth_service

DADOS_VALIDOS = {"nome": "Maria Souza", "email": "Maria@Exemplo.com", "telefone": "(54) 99999-1234", "senha": "senha123"}


@pytest.fixture
def cliente(db, agora):
    return auth_service.cadastrar_cliente(db, ClienteCadastro(**DADOS_VALIDOS), agora)


# --- Cadastro ------------------------------------------------------------------------------------

def test_cadastro_valido_cria_usuario_e_cliente_vinculado(db, cliente):
    assert cliente.tipo == TipoUsuario.CLIENTE
    assert cliente.email == "maria@exemplo.com"
    assert cliente.senha_hash != "senha123"
    assert security.verificar_senha("senha123", cliente.senha_hash)

    registro = db.get(Cliente, cliente.id)
    assert registro is not None
    assert registro.telefone == "(54) 99999-1234"
    assert registro.cpf is None  # CPF só é exigido na contratação de plano (RF05)


def test_cadastro_com_email_repetido_e_recusado(db, cliente, agora):
    with pytest.raises(auth_service.EmailJaCadastrado):
        auth_service.cadastrar_cliente(db, ClienteCadastro(**{**DADOS_VALIDOS, "email": "maria@exemplo.com"}), agora)


@pytest.mark.parametrize(
    "campo, valor",
    [("email", "nao-e-email"), ("senha", "curta"), ("telefone", "abc"), ("nome", " ")],
)
def test_cadastro_com_dado_invalido_e_recusado(campo, valor):
    with pytest.raises(ValidationError):
        ClienteCadastro(**{**DADOS_VALIDOS, campo: valor})


# --- Login ---------------------------------------------------------------------------------------

def test_login_com_credenciais_corretas_retorna_jwt_com_tipo(db, cliente, agora):
    token = auth_service.autenticar(db, Login(email="MARIA@exemplo.com", senha="senha123"), agora)
    payload = security.decodificar_token_acesso(token, agora)
    assert payload["sub"] == str(cliente.id)
    assert payload["tipo"] == "cliente"


@pytest.mark.parametrize("email, senha", [("maria@exemplo.com", "senhaErrada"), ("ninguem@exemplo.com", "senha123")])
def test_login_com_credenciais_incorretas_falha_sem_indicar_o_motivo(db, cliente, agora, email, senha):
    with pytest.raises(auth_service.CredenciaisInvalidas):
        auth_service.autenticar(db, Login(email=email, senha=senha), agora)


# --- Redefinição de senha ------------------------------------------------------------------------

def _token_do_link(link: str) -> str:
    return link.rsplit("/", 1)[-1]


def test_solicitar_redefinicao_para_email_inexistente_nao_gera_link(db, agora):
    dados = SolicitacaoRedefinicaoSenha(email="ninguem@exemplo.com")
    assert auth_service.solicitar_redefinicao_senha(db, dados, "http://teste", agora) is None


def test_redefinir_senha_dentro_do_prazo(db, cliente, agora):
    link = auth_service.solicitar_redefinicao_senha(
        db, SolicitacaoRedefinicaoSenha(email="maria@exemplo.com"), "http://teste/", agora
    )
    assert link.startswith("http://teste/redefinir-senha/")

    depois = agora + timedelta(minutes=settings.RESET_TOKEN_EXPIRE_MINUTES - 1)
    auth_service.redefinir_senha(db, ConfirmacaoRedefinicaoSenha(token=_token_do_link(link), nova_senha="novaSenha456"), depois)

    auth_service.autenticar(db, Login(email="maria@exemplo.com", senha="novaSenha456"), depois)
    with pytest.raises(auth_service.CredenciaisInvalidas):
        auth_service.autenticar(db, Login(email="maria@exemplo.com", senha="senha123"), depois)


def test_redefinir_senha_com_link_expirado_falha(db, cliente, agora):
    link = auth_service.solicitar_redefinicao_senha(
        db, SolicitacaoRedefinicaoSenha(email="maria@exemplo.com"), "http://teste", agora
    )
    expirado = agora + timedelta(minutes=settings.RESET_TOKEN_EXPIRE_MINUTES + 1)
    with pytest.raises(security.TokenExpirado):
        auth_service.redefinir_senha(db, ConfirmacaoRedefinicaoSenha(token=_token_do_link(link), nova_senha="novaSenha456"), expirado)


def test_link_de_redefinicao_so_pode_ser_usado_uma_vez(db, cliente, agora):
    link = auth_service.solicitar_redefinicao_senha(
        db, SolicitacaoRedefinicaoSenha(email="maria@exemplo.com"), "http://teste", agora
    )
    dados = ConfirmacaoRedefinicaoSenha(token=_token_do_link(link), nova_senha="novaSenha456")
    auth_service.redefinir_senha(db, dados, agora)
    with pytest.raises(security.TokenInvalido):
        auth_service.redefinir_senha(db, dados, agora + timedelta(minutes=1))
