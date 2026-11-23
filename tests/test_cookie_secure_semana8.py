# Bandeira Secure do cookie de login (auth.py): depende de DEBUG, que o conftest fixa em True para a suíte
import pytest

from app.core.config import settings
from app.routers.auth import COOKIE_TOKEN
from tests.fabricas import SENHA, criar_cliente


def _set_cookie_do_login(client, db) -> str:
    usuario = criar_cliente(db, email="maria@exemplo.com")
    resposta = client.post("/login", data={"email": usuario.email, "senha": SENHA}, follow_redirects=False)
    assert resposta.status_code == 303
    cabecalho = resposta.headers["set-cookie"]
    assert cabecalho.startswith(f"{COOKIE_TOKEN}=")
    return cabecalho


def _atributos(cabecalho: str) -> set[str]:
    return {parte.strip().split("=")[0].lower() for parte in cabecalho.split(";")[1:]}


def test_cookie_de_login_e_secure_com_debug_desligado(client, db, monkeypatch):
    monkeypatch.setattr(settings, "DEBUG", False)

    atributos = _atributos(_set_cookie_do_login(client, db))

    assert "secure" in atributos
    assert {"httponly", "samesite"} <= atributos
    assert "max-age" not in atributos
    assert "expires" not in atributos


def test_cookie_de_login_nao_e_secure_com_debug_ligado(client, db, monkeypatch):
    monkeypatch.setattr(settings, "DEBUG", True)

    atributos = _atributos(_set_cookie_do_login(client, db))

    assert "secure" not in atributos
    assert {"httponly", "samesite"} <= atributos
    assert "max-age" not in atributos
    assert "expires" not in atributos


def test_cookie_de_login_e_de_sessao_e_mantem_o_acesso(client, db):
    cabecalho = _set_cookie_do_login(client, db)

    atributos = _atributos(cabecalho)
    assert "max-age" not in atributos and "expires" not in atributos

    for rota in ["/", "/consultas", "/historico", "/perfil"]:
        resposta = client.get(rota, follow_redirects=False)
        assert resposta.status_code == 200, rota
    assert "Maria" in client.get("/").text


@pytest.mark.parametrize("debug", [True, False], ids=["debug-ligado", "debug-desligado"])
def test_logout_apaga_o_cookie_com_os_mesmos_atributos_do_login(debug, client, db, monkeypatch):
    monkeypatch.setattr(settings, "DEBUG", debug)
    login = _atributos(_set_cookie_do_login(client, db))

    logout = client.post("/logout", follow_redirects=False)
    cabecalho = logout.headers["set-cookie"]
    assert cabecalho.startswith(f'{COOKIE_TOKEN}=""') or cabecalho.startswith(f"{COOKIE_TOKEN}=;")

    apagar = _atributos(cabecalho)
    assert {"httponly", "samesite", "path", "max-age"} <= apagar
    assert ("secure" in apagar) is ("secure" in login) is (not debug)
    assert "samesite=lax" in cabecalho.lower() and "path=/" in cabecalho.lower()


@pytest.mark.parametrize("debug", [True, False], ids=["debug-ligado", "debug-desligado"])
def test_debug_da_suite_nao_vem_do_env(debug, monkeypatch):
    # A trava do conftest: o DEBUG lido pela suíte é o do ambiente fixado lá, não o do .env
    from app.core.config import Settings

    monkeypatch.setenv("DEBUG", str(debug))

    assert Settings().DEBUG is debug
