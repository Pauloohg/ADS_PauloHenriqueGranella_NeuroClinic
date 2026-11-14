import logging
import re
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.core import paginas_erro
from app.core.config import DADOS_CLINICA
from app.core.paginas_erro import TEXTO_404, html_minimo, linha_de_contato, pede_html, texto_do_erro
from app.db.session import get_db
from app.main import app as fastapi_app
from app.models.cliente import Cliente
from app.models.sessao import StatusSessao
from app.models.usuario import Usuario
from tests.abacatepay_falsa import SECRET_WEBHOOK_TESTE
from tests.fabricas import (
    criar_admin,
    criar_cliente,
    criar_especialidade,
    criar_funcionario,
    criar_paciente,
    criar_plano_com_sessao,
    logar,
)

HTML = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
SEM_HTML = [{}, {"Accept": "*/*"}, {"Accept": "application/json"}]
ROTA_COM_ERRO = "/__teste/erro-inesperado"
MENSAGEM_SECRETA = "segredo-interno-xyz C:\\app\\services\\algo.py"
TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates"


def _principal(html: str) -> str:
    return html[html.index("<main"):html.index("</main>")]


def _titulo(html: str) -> str:
    return re.search(r'<h1[^>]*id="erro-titulo"[^>]*>([^<]*)</h1>', html).group(1)


@pytest.fixture
def cliente(db):
    return criar_cliente(db)


@pytest.fixture
def logado_cliente(client, db, cliente):
    logar(client, cliente.email)
    return client


@pytest.fixture
def profissional(db):
    return criar_funcionario(db, criar_especialidade(db))


@pytest.fixture
def cliente_500(db):
    def explodir():
        raise RuntimeError(MENSAGEM_SECRETA)

    fastapi_app.add_api_route(ROTA_COM_ERRO, explodir, methods=["GET"])
    fastapi_app.dependency_overrides[get_db] = lambda: db
    try:
        with TestClient(fastapi_app, raise_server_exceptions=False) as c:
            yield c
    finally:
        fastapi_app.router.routes[:] = [r for r in fastapi_app.router.routes if getattr(r, "path", None) != ROTA_COM_ERRO]
        fastapi_app.dependency_overrides.clear()


# --- Funções puras ---------------------------------------------------------------------------------

@pytest.mark.parametrize("accept, esperado", [
    ("text/html", True),
    ("text/html,application/xhtml+xml", True),
    ("text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", True),
    ("application/xhtml+xml, TEXT/HTML ; q=0.5", True),
    ("text/html;q=0", False),
    ("text/html;q=abc", True),
    ("*/*", False),
    ("text/*", False),
    ("application/json", False),
    ("application/json, text/plain, */*", False),
    ("", False),
    (None, False),
])
def test_pede_html(accept, esperado):
    assert pede_html(accept) is esperado


@pytest.mark.parametrize("status_code, titulo", [
    (403, "Acesso não permitido"), (404, "Página não encontrada"), (405, "Ação não permitida"),
    (422, "Endereço inválido"), (500, "Algo deu errado do nosso lado"), (503, "Algo deu errado do nosso lado"),
    (418, "Não foi possível abrir esta página"),
])
def test_texto_do_erro_por_status(status_code, titulo):
    erro = texto_do_erro(status_code)
    assert erro.titulo == titulo and erro.texto and erro.icone.startswith("bi-")


def test_textos_combinados():
    assert texto_do_erro(404).texto == (
        "Não encontramos o que você procurou. O endereço pode estar errado ou o conteúdo não está disponível para a sua conta."
    )
    assert texto_do_erro(403).texto == "Esta página não está disponível para o seu tipo de conta."
    assert texto_do_erro(405).texto == "Não foi possível fazer isso por aqui."
    assert texto_do_erro(422).texto == "O endereço ou os dados enviados não são válidos."
    assert texto_do_erro(500).texto.startswith("Tente de novo em instantes. Se você estava confirmando um plano")


def test_linha_de_contato_vem_de_dados_clinica():
    linha = linha_de_contato().replace("\u00a0", " ")
    assert DADOS_CLINICA["telefone"] in linha and DADOS_CLINICA["horario"] in linha
    assert "(11)\u00a05555-0000" in linha_de_contato({**DADOS_CLINICA, "telefone": "(11) 5555-0000"})


def test_um_unico_template_de_erro_sem_js_e_sem_contato_escrito():
    assert [p.name for p in (TEMPLATES / "erros").iterdir()] == ["erro.html"]
    conteudo = (TEMPLATES / "erros" / "erro.html").read_text(encoding="utf-8")
    assert "<script" not in conteudo and DADOS_CLINICA["telefone"] not in conteudo and "telefone pelo" not in conteudo


def test_html_minimo_nao_depende_de_template():
    assert "Página não encontrada" in html_minimo(404) and 'href="/"' in html_minimo(404)


# --- 404 -------------------------------------------------------------------------------------------

def test_404_de_rota_inexistente_com_html(client):
    r = client.get("/rota-que-nao-existe", headers=HTML)
    assert r.status_code == 404 and r.headers["content-type"].startswith("text/html")
    assert _titulo(r.text) == "Página não encontrada" and TEXTO_404 in r.text
    assert "Ir para a página inicial" in r.text and 'href="/login"' in _principal(r.text)


@pytest.mark.parametrize("cabecalhos", SEM_HTML)
def test_404_de_rota_inexistente_sem_html_continua_json(client, cabecalhos):
    r = client.get("/rota-que-nao-existe", headers=cabecalhos)
    assert r.status_code == 404 and r.json() == {"detail": "Not Found"}


@pytest.mark.parametrize("caminho", ["/pacientes/{id}/editar", "/planos/{id}"])
def test_404_alheio_e_inexistente_tem_a_mesma_pagina(client, db, logado_cliente, caminho):
    alheio = criar_plano_com_sessao(db, 999, criar_especialidade(db), StatusSessao.AGENDADA, email_cliente="outro@exemplo.com")
    id_alheio = alheio.paciente_id if "pacientes" in caminho else alheio.id
    r_alheio = logado_cliente.get(caminho.format(id=id_alheio), headers=HTML)
    r_inexistente = logado_cliente.get(caminho.format(id=987654), headers=HTML)
    assert r_alheio.status_code == r_inexistente.status_code == 404
    assert r_alheio.text == r_inexistente.text
    assert "Pedro" not in r_alheio.text
    assert re.search(r">[^<]*\d[^<]*<", _principal(r_alheio.text)) is None  # nenhum número (id) no texto da página


@pytest.mark.parametrize("cabecalhos", SEM_HTML)
def test_404_alheio_e_inexistente_sem_html_continuam_json(client, db, logado_cliente, cabecalhos):
    alheio = criar_plano_com_sessao(db, 999, criar_especialidade(db), StatusSessao.AGENDADA, email_cliente="outro@exemplo.com")
    r_alheio = logado_cliente.get(f"/planos/{alheio.id}", headers=cabecalhos)
    r_inexistente = logado_cliente.get("/planos/987654", headers=cabecalhos)
    assert r_alheio.status_code == r_inexistente.status_code == 404
    assert r_alheio.headers["content-type"] == r_inexistente.headers["content-type"] == "application/json"


def test_404_do_download_de_avaliacao_mostra_a_pagina(client, logado_cliente):
    r = logado_cliente.get("/avaliacoes/987654/arquivo", headers=HTML)
    assert r.status_code == 404 and _titulo(r.text) == "Página não encontrada"
    assert logado_cliente.get("/avaliacoes/987654/arquivo").json() == {"detail": "Avaliação não encontrada."} or \
        logado_cliente.get("/avaliacoes/987654/arquivo").status_code == 404


# --- 403 -------------------------------------------------------------------------------------------

def test_403_cliente_em_rota_do_admin(client, logado_cliente):
    r = logado_cliente.get("/admin/painel", headers=HTML)
    assert r.status_code == 403 and _titulo(r.text) == "Acesso não permitido"
    assert "Esta página não está disponível para o seu tipo de conta." in r.text
    assert "Voltar ao início" in r.text and 'href="/login"' not in _principal(r.text)


def test_403_profissional_em_rota_do_cliente(client, profissional):
    logar(client, profissional.usuario.email)
    r = client.get("/pacientes", headers=HTML)
    assert r.status_code == 403 and _titulo(r.text) == "Acesso não permitido"


@pytest.mark.parametrize("cabecalhos", SEM_HTML)
def test_403_sem_html_continua_json(client, logado_cliente, cabecalhos):
    r = logado_cliente.get("/admin/painel", headers=cabecalhos)
    assert r.status_code == 403 and r.json() == {"detail": "Acesso não permitido para este perfil."}


# --- 405 e 422 -------------------------------------------------------------------------------------

def test_405_com_html_mantem_o_cabecalho_allow(client):
    r = client.post("/", headers=HTML)
    assert r.status_code == 405 and r.headers["allow"] == "GET"
    assert _titulo(r.text) == "Ação não permitida" and "Não foi possível fazer isso por aqui." in r.text


@pytest.mark.parametrize("cabecalhos", SEM_HTML)
def test_405_sem_html_continua_json(client, cabecalhos):
    r = client.post("/", headers=cabecalhos)
    assert r.status_code == 405 and r.json() == {"detail": "Method Not Allowed"} and r.headers["allow"] == "GET"


def test_422_com_id_invalido_no_caminho(client, logado_cliente):
    r = logado_cliente.get("/planos/²", headers=HTML)
    assert r.status_code == 422 and _titulo(r.text) == "Endereço inválido"
    assert "O endereço ou os dados enviados não são válidos." in r.text and "int_parsing" not in r.text


@pytest.mark.parametrize("cabecalhos", SEM_HTML)
def test_422_sem_html_continua_json(client, logado_cliente, cabecalhos):
    r = logado_cliente.get("/planos/²", headers=cabecalhos)
    assert r.status_code == 422 and r.json()["detail"][0]["loc"] == ["path", "plano_id"]


def test_formulario_mapeado_continua_400_com_o_formulario(client):
    r = client.post("/cadastro", data={"nome": "", "email": "x", "telefone": "", "senha": ""}, headers=HTML)
    assert r.status_code == 400 and "erro-titulo" not in r.text


# --- 500 -------------------------------------------------------------------------------------------

def test_500_mostra_pagina_sem_detalhes_tecnicos(cliente_500, caplog):
    with caplog.at_level(logging.ERROR, logger="app.core.paginas_erro"):
        r = cliente_500.get(ROTA_COM_ERRO, headers=HTML)
    assert r.status_code == 500 and r.headers["cache-control"] == "no-store"
    assert _titulo(r.text) == "Algo deu errado do nosso lado"
    for proibido in ("segredo-interno", "algo.py", "Traceback", "RuntimeError", __file__, "test_paginas_erro", "explodir"):
        assert proibido not in r.text
    contato = re.search(r'<p class="erro-contato">(.*?)</p>', r.text, re.S).group(1)
    assert linha_de_contato() in contato and DADOS_CLINICA["telefone"].replace(" ", "\u00a0") in contato


def test_500_registra_a_excecao_no_log_com_metodo_e_caminho(cliente_500, caplog):
    cliente_500.cookies.set("access_token", "token-que-nao-pode-ir-ao-log")
    with caplog.at_level(logging.ERROR, logger="app.core.paginas_erro"):
        cliente_500.get(ROTA_COM_ERRO + "?senha=abc123", headers=HTML)
    registros = [r for r in caplog.records if r.name == "app.core.paginas_erro"]
    assert len(registros) == 1
    assert registros[0].getMessage() == f"Erro não previsto em GET {ROTA_COM_ERRO}"
    assert registros[0].exc_info and isinstance(registros[0].exc_info[1], RuntimeError)
    assert "token-que-nao-pode-ir-ao-log" not in caplog.text and "abc123" not in caplog.text


def test_500_contato_acompanha_dados_clinica(cliente_500, monkeypatch):
    monkeypatch.setitem(DADOS_CLINICA, "telefone", "(54) 3333-4444")
    r = cliente_500.get(ROTA_COM_ERRO, headers=HTML)
    assert "(54)\u00a03333-4444" in re.search(r'<p class="erro-contato">(.*?)</p>', r.text, re.S).group(1)


@pytest.mark.parametrize("cabecalhos", SEM_HTML)
def test_500_sem_html_continua_como_antes(cliente_500, cabecalhos):
    r = cliente_500.get(ROTA_COM_ERRO, headers=cabecalhos)
    assert r.status_code == 500 and "Algo deu errado do nosso lado" not in r.text
    assert r.headers["content-type"].startswith("text/plain")


def test_500_logado_mostra_o_menu_do_perfil(cliente_500, db, cliente):
    logar(cliente_500, cliente.email)
    r = cliente_500.get(ROTA_COM_ERRO, headers=HTML)
    assert r.status_code == 500 and 'href="/historico"' in r.text and "Voltar ao início" in r.text


# --- Falha ao montar a página ----------------------------------------------------------------------

@pytest.mark.parametrize("status_code, caminho, metodo", [(404, "/nao-existe", "GET"), (405, "/", "POST")])
def test_falha_no_template_devolve_html_minimo(client, monkeypatch, status_code, caminho, metodo):
    def quebrar(*args, **kwargs):
        raise RuntimeError("template quebrado")

    monkeypatch.setattr(paginas_erro.templates, "TemplateResponse", quebrar)
    r = client.request(metodo, caminho, headers=HTML)
    assert r.status_code == status_code and r.text == html_minimo(status_code)
    assert "template quebrado" not in r.text


def test_falha_no_template_do_500_devolve_html_minimo(cliente_500, monkeypatch):
    monkeypatch.setattr(paginas_erro.templates, "TemplateResponse", lambda *a, **k: 1 / 0)
    r = cliente_500.get(ROTA_COM_ERRO, headers=HTML)
    assert r.status_code == 500 and r.text == html_minimo(500) and r.headers["cache-control"] == "no-store"


def test_falha_ao_ler_o_usuario_vira_visitante(client, logado_cliente, monkeypatch):
    def quebrar(*args, **kwargs):
        raise RuntimeError("banco fora do ar")

    monkeypatch.setitem(fastapi_app.dependency_overrides, get_db, quebrar)
    resposta = logado_cliente.get("/nao-existe", headers=HTML)
    assert resposta.status_code == 404 and "Ir para a página inicial" in resposta.text


# --- Menu por perfil -------------------------------------------------------------------------------

def _menu(html: str) -> str:
    return html[html.index("<nav class=\"navbar"):html.index("</nav>")]


def test_menu_do_cliente(client, logado_cliente):
    menu = _menu(logado_cliente.get("/nao-existe", headers=HTML).text)
    assert 'href="/historico"' in menu and 'href="/admin/painel"' not in menu and 'href="/agenda"' not in menu


def test_menu_do_profissional(client, profissional):
    logar(client, profissional.usuario.email)
    r = client.get("/nao-existe", headers=HTML)
    menu = _menu(r.text)
    assert 'href="/agenda"' in menu and 'href="/historico"' not in menu and "Voltar ao início" in r.text


def test_menu_do_admin(client, db):
    logar(client, criar_admin(db).email)
    r = client.get("/nao-existe", headers=HTML)
    menu = _menu(r.text)
    assert 'href="/admin/painel"' in menu and 'href="/historico"' not in menu and "Voltar ao início" in r.text


def test_menu_do_visitante(client):
    menu = _menu(client.get("/nao-existe", headers=HTML).text)
    assert 'href="/cadastro"' in menu and "Sair" not in menu


@pytest.mark.parametrize("token", ["lixo", "a.b.c", "", "x" * 5000])
def test_jwt_invalido_vira_visitante(client, token):
    client.cookies.set("access_token", token)
    r = client.get("/nao-existe", headers=HTML)
    assert r.status_code == 404 and 'href="/cadastro"' in _menu(r.text)


def test_usuario_removido_vira_visitante(client, db, logado_cliente, cliente):
    db.execute(delete(Cliente).where(Cliente.usuario_id == cliente.id))
    db.execute(delete(Usuario).where(Usuario.id == cliente.id))
    db.commit()
    r = logado_cliente.get("/nao-existe", headers=HTML)
    assert r.status_code == 404 and 'href="/cadastro"' in _menu(r.text) and "Ir para a página inicial" in r.text


def test_entrar_so_para_visitante_no_403_e_404(client):
    assert 'href="/login"' in _principal(client.get("/nao-existe", headers=HTML).text)
    assert 'href="/login"' not in _principal(client.post("/", headers=HTML).text)


# --- Rotas que não passam pela página --------------------------------------------------------------

@pytest.mark.parametrize("cabecalhos", [HTML, {}])
def test_webhook_com_secret_errado_continua_401_em_json(client, api_falsa, cabecalhos):
    r = client.post("/webhooks/abacatepay?webhookSecret=errado", content=b"{}", headers=cabecalhos)
    assert r.status_code == 401 and r.json() == {"mensagem": "não autorizado"}
    assert SECRET_WEBHOOK_TESTE not in r.text


def test_endpoints_json_continuam_em_json(client, db, logado_cliente, cliente):
    fono = criar_funcionario(db, criar_especialidade(db, nome="Psicologia"), email="p@exemplo.com")
    criar_paciente(db, cliente.id, nascimento=date(2018, 5, 3))
    for caminho in (
        f"/contratar/{fono.usuario.id}/disponibilidade",
        f"/contratar/{fono.usuario.id}/simulacao",
        "/consultas/987654/horarios?data=2026-11-20",
    ):
        r = logado_cliente.get(caminho, headers={"Accept": "*/*"})
        assert r.headers["content-type"] == "application/json", caminho
