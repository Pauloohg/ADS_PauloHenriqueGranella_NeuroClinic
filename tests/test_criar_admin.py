import pytest

from app.core import security
from app.models.usuario import TipoUsuario
from app.schemas.usuario import AdminCadastro
from app.services import auth_service
from scripts import criar_admin as script
from tests.fabricas import AGORA, criar_cliente


def _entradas(*valores):
    fila = iter(valores)
    return lambda _rotulo="": next(fila)


def test_cadastrar_admin_cria_usuario_admin_com_senha_em_hash(db):
    admin = auth_service.cadastrar_admin(db, AdminCadastro(nome="Ana", email="Ana@Clinica.com", senha="segura123"), AGORA)
    assert admin.tipo == TipoUsuario.ADMIN
    assert admin.email == "ana@clinica.com"
    assert security.verificar_senha("segura123", admin.senha_hash)


def test_cadastrar_admin_com_email_existente(db):
    criar_cliente(db, email="ana@clinica.com")
    with pytest.raises(auth_service.EmailJaCadastrado):
        auth_service.cadastrar_admin(db, AdminCadastro(nome="Ana", email="ana@clinica.com", senha="segura123"), AGORA)


def test_script_le_e_valida_os_dados_digitados():
    dados = script.ler_dados(_entradas("Ana Admin", "ana@clinica.com"), _entradas("segura123", "segura123"))
    assert (dados.nome, dados.email, dados.senha) == ("Ana Admin", "ana@clinica.com", "segura123")


def test_script_recusa_confirmacao_diferente(capsys):
    assert script.ler_dados(_entradas("Ana", "ana@clinica.com"), _entradas("segura123", "outra1234")) is None
    assert "não conferem" in capsys.readouterr().out


def test_script_mostra_erros_de_validacao_legiveis(capsys):
    assert script.ler_dados(_entradas("Ana", "nao-e-email"), _entradas("123", "123")) is None
    saida = capsys.readouterr().out
    assert "E-mail: Informe um e-mail válido" in saida
    assert "Senha: A senha deve ter pelo menos 8 caracteres." in saida
