from datetime import date, datetime, timezone

import pytest

from app.models.paciente import Paciente
from app.routers import cliente as cliente_router
from app.routers.auth import utc_para_local
from app.schemas.paciente import PacienteDados
from app.services import paciente_service
from tests.fabricas import criar_admin, criar_cliente, criar_especialidade, criar_funcionario, criar_paciente, logar

HOJE = date(2026, 3, 10)


@pytest.fixture
def dono(db):
    return criar_cliente(db, email="dono@exemplo.com")


@pytest.fixture
def outro(db):
    return criar_cliente(db, email="outro@exemplo.com", nome="Outro Cliente")


# --- Service -------------------------------------------------------------------------------------

def test_cliente_pode_ter_varios_pacientes_e_so_lista_os_seus(db, dono, outro):
    paciente_service.cadastrar(db, dono.id, PacienteDados(nome="Pedro", data_nascimento=date(2018, 5, 3)), HOJE)
    paciente_service.cadastrar(db, dono.id, PacienteDados(nome="Ana", data_nascimento=date(2020, 1, 15)), HOJE)
    criar_paciente(db, outro.id, nome="Filho do Outro")

    assert [p.nome for p in paciente_service.listar_do_cliente(db, dono.id)] == ["Ana", "Pedro"]
    assert [p.nome for p in paciente_service.listar_do_cliente(db, outro.id)] == ["Filho do Outro"]


def test_atualizar_paciente_do_proprio_cliente(db, dono):
    paciente = criar_paciente(db, dono.id)
    dados = PacienteDados(nome="Pedro Henrique", data_nascimento=date(2018, 6, 1))
    paciente_service.atualizar(db, paciente.id, dono.id, dados, HOJE)
    assert (paciente.nome, paciente.data_nascimento) == ("Pedro Henrique", date(2018, 6, 1))


def test_paciente_de_outro_cliente_e_tratado_como_inexistente(db, dono, outro):
    paciente = criar_paciente(db, outro.id, nome="Filho do Outro")
    with pytest.raises(paciente_service.PacienteNaoEncontrado):
        paciente_service.buscar_do_cliente(db, paciente.id, dono.id)
    with pytest.raises(paciente_service.PacienteNaoEncontrado):
        dados = PacienteDados(nome="Invadido", data_nascimento=date(2010, 1, 1))
        paciente_service.atualizar(db, paciente.id, dono.id, dados, HOJE)
    assert paciente.nome == "Filho do Outro"


@pytest.mark.parametrize("nascimento", [date(2026, 3, 11), date(1899, 12, 31)])
def test_data_de_nascimento_fora_do_intervalo_e_recusada(db, dono, nascimento):
    with pytest.raises(paciente_service.DataNascimentoInvalida):
        paciente_service.cadastrar(db, dono.id, PacienteDados(nome="Pedro", data_nascimento=nascimento), HOJE)


def test_nascido_hoje_e_aceito(db, dono):
    paciente_service.cadastrar(db, dono.id, PacienteDados(nome="Recém-nascido", data_nascimento=HOJE), HOJE)


# --- Rotas ---------------------------------------------------------------------------------------

def test_cadastrar_paciente_aparece_na_lista(client, db, dono):
    logar(client, dono.email)
    assert "Nenhum paciente cadastrado" in client.get("/pacientes").text

    resposta = client.post("/pacientes/novo", data={"nome": "Pedro Souza", "data_nascimento": "2018-05-03"}, follow_redirects=False)
    assert resposta.status_code == 303
    html = client.get(resposta.headers["location"]).text
    assert "Paciente cadastrado com sucesso." in html
    assert "Pedro Souza" in html and "03/05/2018" in html


def test_editar_paciente(client, db, dono):
    paciente = criar_paciente(db, dono.id)
    logar(client, dono.email)
    assert 'value="2018-05-03"' in client.get(f"/pacientes/{paciente.id}/editar").text

    dados = {"nome": "Pedro Henrique", "data_nascimento": "2018-06-01"}
    resposta = client.post(f"/pacientes/{paciente.id}/editar", data=dados, follow_redirects=False)
    assert resposta.status_code == 303
    db.refresh(paciente)
    assert paciente.nome == "Pedro Henrique"


def test_lista_nao_mostra_pacientes_de_outro_cliente(client, db, dono, outro):
    criar_paciente(db, outro.id, nome="Filho do Outro")
    logar(client, dono.email)
    assert "Filho do Outro" not in client.get("/pacientes").text


def test_editar_paciente_de_outro_cliente_pela_url_retorna_404(client, db, dono, outro):
    paciente = criar_paciente(db, outro.id, nome="Filho do Outro")
    logar(client, dono.email)

    assert client.get(f"/pacientes/{paciente.id}/editar").status_code == 404
    resposta = client.post(f"/pacientes/{paciente.id}/editar", data={"nome": "Invadido", "data_nascimento": "2010-01-01"})
    assert resposta.status_code == 404
    db.refresh(paciente)
    assert paciente.nome == "Filho do Outro"


@pytest.mark.parametrize(
    "dados, mensagem",
    [
        ({"nome": "", "data_nascimento": ""}, "Este campo é obrigatório."),
        ({"nome": "Pedro", "data_nascimento": "2099-01-01"}, "A data de nascimento não pode estar no futuro."),
        ({"nome": "Pedro", "data_nascimento": "03/05/2018"}, "Informe uma data válida."),
    ],
)
def test_formulario_de_paciente_com_erro(client, db, dono, dados, mensagem):
    logar(client, dono.email)
    resposta = client.post("/pacientes/novo", data=dados)
    assert resposta.status_code == 400
    assert mensagem in resposta.text
    assert db.query(Paciente).count() == 0


def test_hoje_do_paciente_usa_a_data_de_brasilia(client, db, dono, monkeypatch):
    # 01:00 UTC do dia 18 = 22:00 do dia 17 em Brasília
    monkeypatch.setattr(cliente_router, "agora_local", lambda: utc_para_local(datetime(2026, 10, 18, 1, 0, tzinfo=timezone.utc)))
    logar(client, dono.email)
    assert 'max="2026-10-17"' in client.get("/pacientes/novo").text

    resposta = client.post("/pacientes/novo", data={"nome": "Bebê", "data_nascimento": "2026-10-18"})
    assert resposta.status_code == 400
    assert "A data de nascimento não pode estar no futuro." in resposta.text

    resposta = client.post("/pacientes/novo", data={"nome": "Bebê", "data_nascimento": "2026-10-17"}, follow_redirects=False)
    assert resposta.status_code == 303
    assert db.query(Paciente).one().data_nascimento == date(2026, 10, 17)


@pytest.mark.parametrize("perfil", ["admin", "funcionario"])
def test_pacientes_sao_bloqueados_para_outros_perfis(client, db, dono, perfil):
    paciente = criar_paciente(db, dono.id)
    usuario = criar_admin(db) if perfil == "admin" else criar_funcionario(db, criar_especialidade(db)).usuario
    logar(client, usuario.email)
    assert client.get("/pacientes").status_code == 403
    assert client.get("/pacientes/novo").status_code == 403
    assert client.get(f"/pacientes/{paciente.id}/editar").status_code == 403


def test_pacientes_exige_login(client):
    resposta = client.get("/pacientes", follow_redirects=False)
    assert (resposta.status_code, resposta.headers["location"]) == (303, "/login")
