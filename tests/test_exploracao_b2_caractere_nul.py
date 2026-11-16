# B2 da exploração de 14/11: \x00 em query ou formulário vira 400 antes de chegar ao banco (o PostgreSQL daria 500)
import json
import re
import typing

import pytest
from fastapi.routing import APIRoute

from app.main import app as fastapi_app
from app.services.pagamento_service import calcular_assinatura
from tests.abacatepay_falsa import SECRET_WEBHOOK_TESTE
from tests.fabricas import criar_admin, criar_cliente, criar_especialidade, criar_funcionario, logar

NUL = "a\x00b"
MSG = "Os dados enviados contêm caracteres inválidos."
HTML = {"Accept": "text/html,application/xhtml+xml"}
ROTAS_SEM_FILTRO = {"/webhooks/abacatepay"}


def _rotas(rotas):
    # No FastAPI 0.141 as rotas de include_router ficam dentro de _IncludedRouter.original_router
    for rota in rotas:
        if isinstance(rota, APIRoute):
            yield rota
        elif hasattr(rota, "original_router"):
            yield from _rotas(rota.original_router.routes)


def _planos(dependente):
    yield dependente
    for sub in dependente.dependencies:
        yield from _planos(sub)


def _e_texto(anotacao) -> bool:
    return anotacao is str or str in typing.get_args(anotacao) or any(_e_texto(a) for a in typing.get_args(anotacao))


def _casos():
    for rota in sorted(_rotas(fastapi_app.routes), key=lambda r: r.path):
        if rota.path in ROTAS_SEM_FILTRO:
            continue
        caminho = re.sub(r"\{[^}]+\}", "1", rota.path)
        for metodo in sorted(rota.methods - {"HEAD"}):
            for dependente in _planos(rota.dependant):
                for origem, parametros in (("query", dependente.query_params), ("form", dependente.body_params)):
                    for p in parametros:
                        if _e_texto(p.field_info.annotation):
                            yield pytest.param(metodo, caminho, origem, p.alias, id=f"{metodo} {rota.path} {origem}:{p.alias}")


CASOS = list(_casos())


def test_a_varredura_encontra_as_rotas_de_formulario_e_de_busca():
    nomes = {c.id for c in CASOS}
    assert len(CASOS) >= 60
    assert {"POST /cadastro form:nome", "POST /pacientes/novo form:nome", "POST /perfil form:nome",
            "POST /contratar/{funcionario_id}/confirmar form:observacao", "POST /admin/funcionarios/novo form:nome",
            "GET /admin/clientes query:q"} <= nomes


@pytest.mark.parametrize("metodo, caminho, origem, campo", CASOS)
def test_nul_em_qualquer_parametro_de_texto_da_400_e_nunca_500(client, metodo, caminho, origem, campo):
    if origem == "query":
        resposta = client.request(metodo, caminho, params={campo: NUL}, follow_redirects=False)
    else:
        resposta = client.request(metodo, caminho, data={campo: NUL}, follow_redirects=False)

    assert resposta.status_code == 400, resposta.text[:200]


def test_nul_na_chave_da_query_tambem_e_recusado(client):
    assert client.get("/login", params={"a\x00": "1"}).status_code == 400


def test_pagina_amigavel_quando_o_navegador_pede_html(client):
    resposta = client.post("/cadastro", data={"nome": NUL, "email": "x@exemplo.com"}, headers=HTML)

    assert resposta.status_code == 400 and "text/html" in resposta.headers["content-type"]
    assert "Dados inválidos" in resposta.text and MSG in resposta.text
    assert "Traceback" not in resposta.text


def test_json_de_sempre_sem_text_html(client):
    resposta = client.post("/cadastro", data={"nome": NUL}, headers={"Accept": "*/*"})

    assert resposta.status_code == 400 and resposta.json() == {"detail": MSG}


def test_recusa_acontece_antes_do_login_e_do_banco(client, db):
    cliente = criar_cliente(db)
    logar(client, cliente.email)

    resposta = client.post("/perfil", data={"nome": NUL, "email": cliente.email, "telefone": "(54) 99999-1234"})

    assert resposta.status_code == 400
    db.expire_all()
    assert "\x00" not in db.get(type(cliente), cliente.id).nome


def test_multipart_com_nul_em_campo_de_texto_e_recusado(client):
    resposta = client.post("/agenda/sessoes/1/comparecimento", data={"extra": NUL},
                           files={"arquivo": ("a.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")})
    assert resposta.status_code == 400


def test_bytes_nul_dentro_do_pdf_enviado_nao_sao_recusados(client, db):
    # PDF é binário: o filtro olha só campos de texto, nunca o conteúdo do arquivo
    funcionario = criar_funcionario(db, criar_especialidade(db))
    logar(client, funcionario.usuario.email)

    resposta = client.post("/agenda/sessoes/999/comparecimento",
                           files={"arquivo": ("a.pdf", b"%PDF-1.4\n\x00\x00\x00\n%%EOF", "application/pdf")})

    assert resposta.status_code == 404  # segue o caminho normal da rota (sessão inexistente)


def test_texto_normal_continua_passando(client):
    assert client.get("/login", params={"proximo": "acentuação ç ñ 😀"}).status_code == 200


def test_webhook_fica_fora_do_filtro_e_mantem_a_autenticacao(client, api_falsa):
    corpo = json.dumps({"event": "transparent.completed", "data": {"transparent": {"id": "pix_x"}}}).encode()
    cabecalhos = {"X-Webhook-Signature": calcular_assinatura(corpo), "Content-Type": "application/json"}

    sem_segredo = client.post("/webhooks/abacatepay", params={"webhookSecret": NUL}, content=corpo, headers=cabecalhos)
    com_segredo = client.post("/webhooks/abacatepay", params={"webhookSecret": SECRET_WEBHOOK_TESTE},
                              content=corpo, headers=cabecalhos)

    assert sem_segredo.status_code == 401 and com_segredo.status_code == 200


def test_rotas_que_ja_davam_400_por_formulario_nao_mudaram(client, db):
    logar(client, criar_admin(db).email)
    resposta = client.post("/admin/especialidades/nova", data={"nome": "", "valor_sessao": ""})

    assert resposta.status_code == 400 and "Dados inválidos" not in resposta.text
