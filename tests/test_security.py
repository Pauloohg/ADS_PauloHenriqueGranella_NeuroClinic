from datetime import timedelta

import pytest

from app.core import security
from app.core.config import settings


def test_hash_de_senha_nao_guarda_texto_puro():
    senha_hash = security.gerar_hash_senha("senhaForte123")
    assert senha_hash != "senhaForte123"
    assert security.verificar_senha("senhaForte123", senha_hash)
    assert not security.verificar_senha("outraSenha123", senha_hash)


def test_token_de_acesso_carrega_tipo_do_usuario(agora):
    token = security.criar_token_acesso(7, "cliente", agora)
    payload = security.decodificar_token_acesso(token, agora)
    assert payload["sub"] == "7"
    assert payload["tipo"] == "cliente"


def test_token_de_acesso_expira(agora):
    token = security.criar_token_acesso(7, "cliente", agora)
    depois = agora + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    with pytest.raises(security.TokenExpirado):
        security.decodificar_token_acesso(token, depois)


def test_token_de_redefinicao_valido_dentro_do_prazo(agora):
    token = security.criar_token_redefinicao(7, "hash-atual", agora)
    quase_expirando = agora + timedelta(minutes=settings.RESET_TOKEN_EXPIRE_MINUTES) - timedelta(seconds=1)
    payload = security.decodificar_token_redefinicao(token, quase_expirando)
    assert payload["sub"] == "7"


def test_token_de_redefinicao_expira_apos_o_prazo(agora):
    token = security.criar_token_redefinicao(7, "hash-atual", agora)
    expirado = agora + timedelta(minutes=settings.RESET_TOKEN_EXPIRE_MINUTES)
    with pytest.raises(security.TokenExpirado):
        security.decodificar_token_redefinicao(token, expirado)


def test_tokens_de_login_e_redefinicao_nao_sao_intercambiaveis(agora):
    acesso = security.criar_token_acesso(7, "cliente", agora)
    redefinicao = security.criar_token_redefinicao(7, "hash-atual", agora)
    with pytest.raises(security.TokenInvalido):
        security.decodificar_token_redefinicao(acesso, agora)
    with pytest.raises(security.TokenInvalido):
        security.decodificar_token_acesso(redefinicao, agora)


def test_token_adulterado_e_rejeitado(agora):
    token = security.criar_token_redefinicao(7, "hash-atual", agora)
    with pytest.raises(security.TokenInvalido):
        security.decodificar_token_redefinicao(token[:-2] + "xx", agora)
