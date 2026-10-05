import re

import pytest
from fastapi.routing import APIRoute

from app import main
from app.main import FORMULARIOS_POR_ROTA, app as fastapi_app
from tests.fabricas import criar_admin, criar_cliente, criar_especialidade, criar_funcionario, logar

LINKS_POR_PERFIL = {
    "cliente": {"/perfil": "Meu Perfil", "/pacientes": "Meus Pacientes"},
    "admin": {"/admin/funcionarios": "Funcionários", "/admin/especialidades": "Especialidades", "/conta/alterar-senha": "Alterar Senha"},
    "funcionario": {"/conta/alterar-senha": "Alterar Senha"},
}
TODOS_OS_LINKS = {href for links in LINKS_POR_PERFIL.values() for href in links}

# POST que não recebem dados digitados pelo usuário (só um botão de ação)
POST_SEM_FORMULARIO = {
    "/logout",
    "/admin/funcionarios/{funcionario_id}/desativar",
    "/admin/funcionarios/{funcionario_id}/reativar",
}


def _menu(html: str) -> str:
    return re.search(r'<ul class="navbar-nav menu-principal.*?</ul>', html, re.S).group(0)


def _logar_perfil(client, db, perfil):
    if perfil == "cliente":
        usuario = criar_cliente(db)
    elif perfil == "admin":
        usuario = criar_admin(db)
    else:
        usuario = criar_funcionario(db, criar_especialidade(db)).usuario
    logar(client, usuario.email)


@pytest.mark.parametrize("perfil", LINKS_POR_PERFIL)
def test_navbar_mostra_so_os_links_do_perfil(client, db, perfil):
    _logar_perfil(client, db, perfil)
    menu = _menu(client.get("/").text)
    for href, rotulo in LINKS_POR_PERFIL[perfil].items():
        assert f'href="{href}"' in menu and rotulo in menu
    for href in TODOS_OS_LINKS - LINKS_POR_PERFIL[perfil].keys():
        assert f'href="{href}"' not in menu


def test_navbar_marca_a_pagina_atual(client, db):
    _logar_perfil(client, db, "cliente")
    menu = _menu(client.get("/pacientes").text)
    assert re.search(r'class="nav-link active" href="/pacientes" aria-current="page"', menu)
    assert 'class="nav-link " href="/perfil"' in menu


def test_visitante_nao_ve_menu_de_perfil(client):
    html = client.get("/").text
    assert "menu-principal" not in html
    for href in TODOS_OS_LINKS:
        assert f'href="{href}"' not in html


def test_toda_rota_post_de_formulario_esta_em_formularios_por_rota():
    rotas_post = {
        rota.path for rota in fastapi_app.routes
        if isinstance(rota, APIRoute) and "POST" in rota.methods and not rota.path.startswith("/_teste")
    }
    faltando = rotas_post - POST_SEM_FORMULARIO - FORMULARIOS_POR_ROTA.keys()
    assert not faltando, f"rotas POST de formulário fora de FORMULARIOS_POR_ROTA: {faltando}"


@pytest.mark.parametrize("template", sorted(set(FORMULARIOS_POR_ROTA.values())))
def test_templates_de_formulario_renderizam_com_o_contexto_do_handler_global(template):
    # Mesmo contexto mínimo que reexibir_formulario_com_erros monta, sem `usuario` nem listas da rota
    contexto = {"dados": {}, "email": "", "token": "", "erros": {"nome": "x"}, "erro": main.MSG_VALIDACAO_GERAL}
    html = main.templates.get_template(template).render(request=_RequestFalso(), **contexto)
    assert main.MSG_VALIDACAO_GERAL in html or "Nenhuma especialidade" in html


class _RequestFalso:
    class url:
        path = "/rota"

    query_params: dict = {}
