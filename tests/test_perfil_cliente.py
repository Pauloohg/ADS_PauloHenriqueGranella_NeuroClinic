import pytest
from pydantic import ValidationError

from app.models.cliente import Cliente
from app.schemas.cliente import PerfilClienteAtualizacao
from app.services import cliente_service
from tests.fabricas import criar_admin, criar_cliente, criar_especialidade, criar_funcionario, logar

CPF_VALIDO = "529.982.247-25"
FORM = {"nome": "Maria Souza Lima", "email": "maria.nova@exemplo.com", "telefone": "(54) 98888-0000", "cpf": CPF_VALIDO}


# --- Schema --------------------------------------------------------------------------------------

@pytest.mark.parametrize("entrada", ["52998224725", "529.982.247-25", " 529 982 247 25 "])
def test_cpf_e_normalizado_para_o_formato_com_pontuacao(entrada):
    assert PerfilClienteAtualizacao(**{**FORM, "cpf": entrada}).cpf == CPF_VALIDO


@pytest.mark.parametrize("cpf", ["529.982.247-24", "111.111.111-11", "1234567890", "abc.def.ghi-jk"])
def test_cpf_invalido_e_recusado(cpf):
    with pytest.raises(ValidationError, match="CPF válido"):
        PerfilClienteAtualizacao(**{**FORM, "cpf": cpf})


def test_cpf_em_branco_continua_opcional():
    assert PerfilClienteAtualizacao(**{**FORM, "cpf": "  "}).cpf is None


# --- Service -------------------------------------------------------------------------------------

def test_atualizar_perfil_salva_todos_os_campos(db):
    usuario = criar_cliente(db)
    cliente_service.atualizar_perfil(db, usuario, PerfilClienteAtualizacao(**FORM))

    cliente = db.get(Cliente, usuario.id)
    assert (usuario.nome, usuario.email) == ("Maria Souza Lima", "maria.nova@exemplo.com")
    assert (cliente.telefone, cliente.cpf) == ("(54) 98888-0000", CPF_VALIDO)


def test_manter_o_proprio_email_nao_conta_como_duplicado(db):
    usuario = criar_cliente(db)
    cliente_service.atualizar_perfil(db, usuario, PerfilClienteAtualizacao(**{**FORM, "email": usuario.email}))


def test_email_de_outro_usuario_e_recusado(db):
    criar_admin(db, email="ocupado@exemplo.com")
    usuario = criar_cliente(db)
    with pytest.raises(cliente_service.DadosEmUso) as exc:
        cliente_service.atualizar_perfil(db, usuario, PerfilClienteAtualizacao(**{**FORM, "email": "Ocupado@exemplo.com"}))
    assert exc.value.campos == {"email"}


def test_cpf_de_outro_cliente_e_recusado(db):
    outro = criar_cliente(db, email="outro@exemplo.com")
    cliente_service.atualizar_perfil(db, outro, PerfilClienteAtualizacao(**{**FORM, "email": "outro@exemplo.com"}))
    usuario = criar_cliente(db)
    with pytest.raises(cliente_service.DadosEmUso) as exc:
        cliente_service.atualizar_perfil(db, usuario, PerfilClienteAtualizacao(**FORM))
    assert exc.value.campos == {"cpf"}


# --- Rotas ---------------------------------------------------------------------------------------

def test_tela_de_perfil_mostra_os_dados_atuais(client, db):
    usuario = criar_cliente(db)
    logar(client, usuario.email)
    html = client.get("/perfil").text
    assert 'value="Maria Souza"' in html
    assert 'value="cliente@exemplo.com"' in html
    assert 'value="(54) 99999-1234"' in html
    assert 'name="cpf"' in html
    assert 'href="/conta/alterar-senha"' in html


def test_post_perfil_atualiza_e_reflete_na_tela(client, db):
    logar(client, criar_cliente(db).email)
    resposta = client.post("/perfil", data=FORM, follow_redirects=False)
    assert resposta.status_code == 303
    html = client.get(resposta.headers["location"]).text
    assert "Dados atualizados com sucesso." in html
    assert f'value="{CPF_VALIDO}"' in html
    assert 'value="maria.nova@exemplo.com"' in html


def test_post_perfil_com_email_de_outro_usuario_reexibe_com_erro(client, db):
    criar_cliente(db, email="ocupado@exemplo.com")
    logar(client, criar_cliente(db).email)
    resposta = client.post("/perfil", data={**FORM, "email": "ocupado@exemplo.com"})
    assert resposta.status_code == 400
    assert "Este e-mail já está em uso por outra conta." in resposta.text
    assert f'value="{CPF_VALIDO}"' in resposta.text  # não perde o que foi digitado


def test_post_perfil_com_campos_invalidos(client, db):
    logar(client, criar_cliente(db).email)
    resposta = client.post("/perfil", data={**FORM, "cpf": "123", "telefone": ""})
    assert resposta.status_code == 400
    assert "Informe um CPF válido." in resposta.text
    assert "Este campo é obrigatório." in resposta.text


@pytest.mark.parametrize("perfil", ["admin", "funcionario"])
def test_perfil_do_cliente_e_bloqueado_para_outros_perfis(client, db, perfil):
    usuario = criar_admin(db) if perfil == "admin" else criar_funcionario(db, criar_especialidade(db)).usuario
    logar(client, usuario.email)
    assert client.get("/perfil").status_code == 403
    assert client.post("/perfil", data=FORM).status_code == 403


def test_perfil_exige_login(client):
    resposta = client.get("/perfil", follow_redirects=False)
    assert (resposta.status_code, resposta.headers["location"]) == (303, "/login")


def test_email_duplicado_aparece_no_campo_mesmo_com_cpf_invalido(client, db):
    criar_cliente(db, email="ocupado@exemplo.com")
    logar(client, criar_cliente(db).email)
    resposta = client.post("/perfil", data={**FORM, "email": "Ocupado@exemplo.com", "cpf": "123"})
    assert resposta.status_code == 400
    assert "Este e-mail já está em uso por outra conta." in resposta.text
    assert "Informe um CPF válido." in resposta.text


def test_email_e_cpf_duplicados_aparecem_juntos(client, db):
    dono = criar_cliente(db, email="ocupado@exemplo.com")
    cliente_service.atualizar_perfil(db, dono, PerfilClienteAtualizacao(**{**FORM, "email": dono.email}))
    logar(client, criar_cliente(db).email)
    resposta = client.post("/perfil", data={**FORM, "email": "ocupado@exemplo.com"})
    assert resposta.status_code == 400
    assert "Este e-mail já está em uso por outra conta." in resposta.text
    assert "Este CPF já está cadastrado em outra conta." in resposta.text
