import re
from datetime import datetime, timezone

from app.schemas.usuario import ClienteCadastro
from app.services import auth_service
from tests.fabricas import criar_especialidade, criar_funcionario, logar

FORM_CADASTRO = {"nome": "João Lima", "email": "joao@exemplo.com", "telefone": "54 98888-0000", "senha": "senha123"}


def _html_normalizado(resposta) -> str:
    return " ".join(resposta.text.split())


def _tem_link(html: str, href: str, texto: str) -> bool:
    return re.search(rf'<a href="{re.escape(href)}"[^>]*>\s*{re.escape(texto)}\s*</a>', html) is not None


def test_home_visitante_mostra_landing_institucional(client):
    resposta = client.get("/")
    assert resposta.status_code == 200
    html = _html_normalizado(resposta)
    for texto in [
        "Atendimento multidisciplinar",
        "planos de atendimento recorrentes",
        "Seu plano",
        "Exemplo ilustrativo",
        "Por que escolher a NeuroClinic",
        "O que oferecemos",
        "Psicopedagogia",
        "Fonoaudiologia",
        "Psicologia",
        "Como funciona",
        "Pronto para começar?",
    ]:
        assert texto in html, texto
    assert "menu-principal" not in html


def test_home_visitante_mostra_botoes_de_cta(client):
    html = _html_normalizado(client.get("/"))
    assert _tem_link(html, "/cadastro", "Agendar avaliação")
    assert _tem_link(html, "/login", "Já sou cliente")
    assert _tem_link(html, "/cadastro", "Criar minha conta")


def test_home_autenticado_mostra_o_inicio_sem_landing(client, db):
    auth_service.cadastrar_cliente(db, ClienteCadastro(**FORM_CADASTRO), datetime.now(timezone.utc))
    client.post("/login", data={"email": "joao@exemplo.com", "senha": "senha123"})

    html = _html_normalizado(client.get("/"))
    assert re.search(r"(Bom dia|Boa tarde|Boa noite), João", html)
    assert "Por que escolher a NeuroClinic" not in html
    assert 'href="/login"' not in html
    assert 'href="/cadastro"' not in html


def test_home_calendario_exemplo_marca_sessoes_por_especialidade(client):
    html = _html_normalizado(client.get("/"))
    for especialidade in ["psicopedagogia", "fonoaudiologia", "psicologia"]:
        assert f'class="sessao esp-{especialidade}"' in html
        assert f'card card-especialidade esp-{especialidade}' in html


def test_home_do_funcionario_usa_o_estilo_das_telas_autenticadas(client, db):
    logar(client, criar_funcionario(db, criar_especialidade(db)).usuario.email)
    html = _html_normalizado(client.get("/"))
    assert re.search(r'<main class="[^"]*\bpagina-interna\b', html)
    assert re.search(r'<h1 class="[^"]*\btitulo-serifa\b[^"]*">\s*(Bom dia|Boa tarde|Boa noite), Carlos<', html)
