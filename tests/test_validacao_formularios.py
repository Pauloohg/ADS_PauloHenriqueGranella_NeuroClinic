from typing import Annotated

import pytest
from fastapi import Form

from app import main
from app.main import MSG_VALIDACAO_GERAL, app as fastapi_app
from app.routers.auth import (
    MSG_CAMPO_OBRIGATORIO,
    MSG_EMAIL_INVALIDO,
    MSG_ERRO_GENERICO,
    erros_por_campo,
    mensagem_erro,
)

FORM_VALIDO = {"nome": "Ana Lima", "email": "ana@exemplo.com", "telefone": "(54) 99999-0000", "senha": "senha123"}

# Termos que denunciam erro técnico vazando para a tela
TERMOS_TECNICOS = ["Traceback", "KeyError", "value_error", "Value error", "ctx", "missing", "Internal Server Error"]


def _sem_termos_tecnicos(html: str):
    for termo in TERMOS_TECNICOS:
        assert termo not in html, f"termo técnico {termo!r} exibido ao usuário"


# --- Rota /cadastro ------------------------------------------------------------------------------

def test_cadastro_com_todos_os_campos_vazios(client):
    resposta = client.post("/cadastro", data={"nome": "", "email": "", "telefone": "", "senha": ""})
    assert resposta.status_code == 400
    assert resposta.text.count(MSG_CAMPO_OBRIGATORIO) == 4
    _sem_termos_tecnicos(resposta.text)


def test_cadastro_sem_enviar_nenhum_campo(client):
    resposta = client.post("/cadastro", data={})
    assert resposta.status_code == 400
    assert MSG_CAMPO_OBRIGATORIO in resposta.text
    _sem_termos_tecnicos(resposta.text)


@pytest.mark.parametrize("campo", ["nome", "email", "telefone", "senha"])
def test_cadastro_com_um_campo_obrigatorio_vazio(client, campo):
    resposta = client.post("/cadastro", data={**FORM_VALIDO, campo: ""})
    assert resposta.status_code == 400
    assert resposta.text.count(MSG_CAMPO_OBRIGATORIO) == 1
    _sem_termos_tecnicos(resposta.text)


@pytest.mark.parametrize("email", ["ana", "ana@", "ana@exemplo", "@exemplo.com", "ana exemplo.com"])
def test_cadastro_com_email_invalido(client, email):
    resposta = client.post("/cadastro", data={**FORM_VALIDO, "email": email})
    assert resposta.status_code == 400
    assert MSG_EMAIL_INVALIDO in resposta.text
    _sem_termos_tecnicos(resposta.text)


def test_cadastro_com_senha_curta(client):
    resposta = client.post("/cadastro", data={**FORM_VALIDO, "senha": "abc"})
    assert resposta.status_code == 400
    assert "A senha deve ter pelo menos 8 caracteres." in resposta.text
    _sem_termos_tecnicos(resposta.text)


def test_cadastro_com_sucesso(client):
    resposta = client.post("/cadastro", data=FORM_VALIDO, follow_redirects=False)
    assert resposta.status_code == 303
    assert resposta.headers["location"] == "/login?cadastro=ok"


# --- Tradução das mensagens ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "campo, erro, esperado",
    [
        ("nome", {"type": "missing", "loc": ("nome",)}, MSG_CAMPO_OBRIGATORIO),  # sem "ctx" nem "input"
        ("email", {"type": "value_error", "input": "x", "ctx": {"reason": "sem @"}}, MSG_EMAIL_INVALIDO),
        ("telefone", {"type": "value_error", "input": "x", "ctx": {}}, MSG_ERRO_GENERICO),  # ctx sem "error"
        ("telefone", {"type": "value_error", "input": "x"}, MSG_ERRO_GENERICO),  # sem "ctx"
        ("nome", {"type": "string_too_short", "input": "A", "ctx": {"min_length": 2}}, "Deve ter pelo menos 2 caracteres."),
        ("nome", {"type": "tipo_que_nao_existe", "input": 123}, MSG_ERRO_GENERICO),
        ("nome", {}, MSG_ERRO_GENERICO),
    ],
)
def test_mensagem_erro_nao_depende_de_ctx(campo, erro, esperado):
    assert mensagem_erro(campo, erro) == esperado


def test_erros_por_campo_ignora_prefixo_body_do_fastapi():
    erros = erros_por_campo([{"type": "missing", "loc": ("body", "email")}, {"type": "missing"}])
    assert erros == {"email": MSG_CAMPO_OBRIGATORIO, "geral": MSG_CAMPO_OBRIGATORIO}


# --- Handler global de RequestValidationError ----------------------------------------------------

@fastapi_app.post("/_teste/form-obrigatorio")
def _rota_com_form_obrigatorio(nome: Annotated[str, Form()], senha: Annotated[str, Form()]):
    return {"ok": True}


def test_handler_global_reexibe_formulario_com_mensagem_amigavel(client, monkeypatch):
    monkeypatch.setitem(main.FORMULARIOS_POR_ROTA, "/_teste/form-obrigatorio", "auth/cadastro.html")
    resposta = client.post("/_teste/form-obrigatorio", data={"email": "ana@exemplo.com", "senha": "segredo123"})
    assert resposta.status_code == 400
    assert MSG_VALIDACAO_GERAL in resposta.text
    assert MSG_CAMPO_OBRIGATORIO in resposta.text  # "nome" faltando, marcado no campo
    assert 'value="ana@exemplo.com"' in resposta.text  # preserva o que foi digitado
    assert "segredo123" not in resposta.text  # mas nunca devolve a senha
    _sem_termos_tecnicos(resposta.text)


def test_handler_global_mantem_422_para_rotas_sem_formulario(client):
    resposta = client.post("/_teste/form-obrigatorio", data={})
    assert resposta.status_code == 422
    assert resposta.headers["content-type"].startswith("application/json")
