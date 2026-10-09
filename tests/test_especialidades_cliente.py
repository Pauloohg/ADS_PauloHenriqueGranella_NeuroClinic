import pytest

from app.services import funcionario_service
from tests.fabricas import AGORA, criar_admin, criar_cliente, criar_especialidade, criar_funcionario, logar


@pytest.fixture
def fono(db):
    return criar_especialidade(db, "Fonoaudiologia", "150.00")


@pytest.fixture
def equipe_fono(db, fono):
    ana = criar_funcionario(db, fono, email="ana@exemplo.com", nome="Ana Fono")
    bruno = criar_funcionario(db, fono, email="bruno@exemplo.com", nome="Bruno Fono")
    inativo = criar_funcionario(db, fono, email="caio@exemplo.com", nome="Caio Inativo")
    funcionario_service.desativar(db, inativo.usuario.id, AGORA.replace(tzinfo=None))
    return ana, bruno, inativo


@pytest.fixture
def cliente_logado(client, db):
    logar(client, criar_cliente(db).email)


# --- Service -------------------------------------------------------------------------------------

def test_lista_so_profissionais_ativos_da_especialidade(db, fono, equipe_fono):
    psico = criar_especialidade(db, "Psicologia")
    criar_funcionario(db, psico, email="paula@exemplo.com", nome="Paula Psico")

    nomes = [u.nome for u in funcionario_service.listar_ativos_da_especialidade(db, fono.id)]
    assert nomes == ["Ana Fono", "Bruno Fono"]


def test_profissional_reativado_volta_a_aparecer(db, fono, equipe_fono):
    inativo = equipe_fono[2]
    funcionario_service.reativar(db, inativo.usuario.id)

    nomes = [u.nome for u in funcionario_service.listar_ativos_da_especialidade(db, fono.id)]
    assert nomes == ["Ana Fono", "Bruno Fono", "Caio Inativo"]


def test_especialidade_sem_profissionais_retorna_lista_vazia(db, fono):
    assert funcionario_service.listar_ativos_da_especialidade(db, fono.id) == []


# --- Rotas ---------------------------------------------------------------------------------------

def test_cliente_ve_especialidades_com_valor_e_chip(client, db, fono, cliente_logado):
    psico = criar_especialidade(db, "Psicologia", "1250.00")
    html = client.get("/especialidades").text

    assert 'class="chip-especialidade align-self-start">Fonoaudiologia' in html
    assert "esp-fonoaudiologia" in html and "esp-psicologia" in html
    assert "R$ 150,00" in html and "R$ 1.250,00" in html
    assert f'href="/especialidades/{psico.id}/profissionais"' in html
    assert "Contratar" not in html.split("<main", 1)[1]  # a navbar tem "Contratar plano"; o conteúdo da página não


def test_cliente_sem_especialidades_ve_estado_vazio(client, cliente_logado):
    assert "Nenhuma especialidade disponível no momento" in client.get("/especialidades").text


def test_cliente_ve_so_profissionais_ativos(client, db, fono, equipe_fono, cliente_logado):
    resposta = client.get(f"/especialidades/{fono.id}/profissionais")

    assert resposta.status_code == 200
    assert "Ana Fono" in resposta.text and "Bruno Fono" in resposta.text
    assert "Caio Inativo" not in resposta.text
    assert "Nenhum profissional disponível" not in resposta.text
    ana, bruno, inativo = equipe_fono
    for ativo in (ana, bruno):
        assert f'href="/contratar/{ativo.usuario.id}"' in resposta.text
    assert f'href="/contratar/{inativo.usuario.id}"' not in resposta.text


def test_reativado_volta_a_aparecer_na_tela(client, db, fono, equipe_fono, cliente_logado):
    funcionario_service.reativar(db, equipe_fono[2].usuario.id)
    assert "Caio Inativo" in client.get(f"/especialidades/{fono.id}/profissionais").text


def test_especialidade_so_com_inativos_mostra_estado_vazio(client, db, fono, cliente_logado):
    inativo = criar_funcionario(db, fono)
    funcionario_service.desativar(db, inativo.usuario.id, AGORA.replace(tzinfo=None))

    html = client.get(f"/especialidades/{fono.id}/profissionais").text
    assert "Nenhum profissional disponível no momento" in html
    assert "Carlos Fono" not in html


def test_especialidade_inexistente_retorna_404(client, cliente_logado):
    assert client.get("/especialidades/999/profissionais").status_code == 404


def test_navbar_marca_profissionais_como_ativo(client, db, fono, cliente_logado):
    for url in ("/especialidades", f"/especialidades/{fono.id}/profissionais"):
        assert 'class="nav-link active" href="/especialidades" aria-current="page"' in client.get(url).text


@pytest.mark.parametrize("perfil", ["admin", "funcionario"])
def test_outros_perfis_recebem_403(client, db, fono, perfil):
    if perfil == "admin":
        email = criar_admin(db).email
    else:
        email = criar_funcionario(db, fono).usuario.email
    logar(client, email)

    assert client.get("/especialidades").status_code == 403
    assert client.get(f"/especialidades/{fono.id}/profissionais").status_code == 403


def test_sem_login_redireciona_para_login(client, fono):
    for url in ("/especialidades", f"/especialidades/{fono.id}/profissionais"):
        resposta = client.get(url, follow_redirects=False)
        assert resposta.status_code == 303 and resposta.headers["location"] == "/login"
