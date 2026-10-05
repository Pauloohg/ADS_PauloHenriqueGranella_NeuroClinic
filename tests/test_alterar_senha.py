import pytest
from pydantic import ValidationError

from app.core import security
from app.routers.conta import MSG_SENHA_ATUAL_INCORRETA
from app.schemas.usuario import AlteracaoSenha
from app.services import auth_service
from tests.fabricas import SENHA, criar_admin, criar_cliente, criar_especialidade, criar_funcionario, logar

NOVA = "novaSenha456"
FORM_OK = {"senha_atual": SENHA, "nova_senha": NOVA, "confirmacao_senha": NOVA}


# --- Service / schema ----------------------------------------------------------------------------

def test_alterar_senha_com_senha_atual_correta(db):
    usuario = criar_cliente(db)
    auth_service.alterar_senha(db, usuario, AlteracaoSenha(**FORM_OK))
    assert security.verificar_senha(NOVA, usuario.senha_hash)
    assert not security.verificar_senha(SENHA, usuario.senha_hash)


def test_alterar_senha_rejeita_senha_atual_errada(db):
    usuario = criar_cliente(db)
    hash_antes = usuario.senha_hash
    with pytest.raises(auth_service.SenhaAtualIncorreta):
        auth_service.alterar_senha(db, usuario, AlteracaoSenha(**{**FORM_OK, "senha_atual": "outraSenha1"}))
    assert usuario.senha_hash == hash_antes


def test_confirmacao_diferente_e_recusada_no_campo_de_confirmacao():
    with pytest.raises(ValidationError) as exc:
        AlteracaoSenha(**{**FORM_OK, "confirmacao_senha": "diferente123"})
    assert exc.value.errors()[0]["loc"] == ("confirmacao_senha",)


def test_nova_senha_usa_a_mesma_regra_de_forca_do_cadastro():
    with pytest.raises(ValidationError, match="pelo menos 8 caracteres"):
        AlteracaoSenha(senha_atual=SENHA, nova_senha="curta", confirmacao_senha="curta")


# --- Rotas ---------------------------------------------------------------------------------------

def _usuario_de_cada_perfil(db, perfil):
    if perfil == "cliente":
        return criar_cliente(db)
    if perfil == "admin":
        return criar_admin(db)
    return criar_funcionario(db, criar_especialidade(db)).usuario


@pytest.mark.parametrize("perfil", ["cliente", "funcionario", "admin"])
def test_os_tres_perfis_alteram_a_propria_senha(client, db, perfil):
    usuario = _usuario_de_cada_perfil(db, perfil)
    logar(client, usuario.email)
    assert client.get("/conta/alterar-senha").status_code == 200

    resposta = client.post("/conta/alterar-senha", data=FORM_OK, follow_redirects=False)
    assert resposta.status_code == 303
    assert "Senha alterada com sucesso" in client.get(resposta.headers["location"]).text

    client.post("/logout")
    logar(client, usuario.email, NOVA)


def test_rota_rejeita_senha_atual_errada_sem_trocar_a_senha(client, db):
    usuario = criar_cliente(db)
    logar(client, usuario.email)
    resposta = client.post("/conta/alterar-senha", data={**FORM_OK, "senha_atual": "errada999"})
    assert resposta.status_code == 400
    assert MSG_SENHA_ATUAL_INCORRETA in resposta.text
    assert "errada999" not in resposta.text and NOVA not in resposta.text  # senhas nunca voltam no HTML

    db.refresh(usuario)
    assert security.verificar_senha(SENHA, usuario.senha_hash)


def test_rota_mostra_erro_de_confirmacao(client, db):
    logar(client, criar_cliente(db).email)
    resposta = client.post("/conta/alterar-senha", data={**FORM_OK, "confirmacao_senha": "outra12345"})
    assert resposta.status_code == 400
    assert "A confirmação não confere com a nova senha." in resposta.text


def test_alterar_senha_exige_login(client):
    resposta = client.get("/conta/alterar-senha", follow_redirects=False)
    assert resposta.status_code == 303
    assert resposta.headers["location"] == "/login"
    assert client.post("/conta/alterar-senha", data=FORM_OK, follow_redirects=False).headers["location"] == "/login"
