from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.core import security
from app.models.funcionario import Funcionario
from app.models.sessao import StatusSessao
from app.models.usuario import TipoUsuario, Usuario
from app.routers import admin as admin_router
from app.routers.auth import utc_para_local
from app.schemas.funcionario import FuncionarioCadastro, FuncionarioEdicao
from app.services import auth_service, especialidade_service, funcionario_service
from tests.fabricas import (
    AGORA,
    criar_admin,
    criar_cliente,
    criar_especialidade,
    criar_funcionario,
    criar_plano_com_sessao,
    logar,
)

FORM = {"nome": "Carlos Fono", "email": "carlos@exemplo.com", "senha": "provisoria1"}

AGORA_RN = datetime(2026, 10, 10, 12, 0)  # horário local de Brasília, sem fuso, como Sessao.data_hora
FUTURO = datetime(2026, 10, 17, 12, 0)
PASSADO = datetime(2026, 10, 3, 12, 0)


@pytest.fixture
def fono(db):
    return criar_especialidade(db, "Fonoaudiologia")


@pytest.fixture
def admin_logado(client, db):
    logar(client, criar_admin(db).email)


@pytest.fixture
def relogio_fixo(monkeypatch):
    monkeypatch.setattr(admin_router, "agora_local", lambda: AGORA_RN)


# --- Service -------------------------------------------------------------------------------------

def test_cadastrar_funcionario_cria_usuario_com_senha_provisoria_em_hash(db, fono):
    resumo = funcionario_service.cadastrar(db, FuncionarioCadastro(**FORM, especialidade_id=fono.id), AGORA)

    assert resumo.usuario.tipo == TipoUsuario.FUNCIONARIO
    assert resumo.usuario.senha_hash != "provisoria1"
    assert security.verificar_senha("provisoria1", resumo.usuario.senha_hash)
    assert (resumo.funcionario.especialidade_id, resumo.funcionario.ativo) == (fono.id, True)


def test_senha_provisoria_usa_a_mesma_regra_de_forca():
    with pytest.raises(ValidationError, match="pelo menos 8 caracteres"):
        FuncionarioCadastro(**{**FORM, "senha": "123"}, especialidade_id=1)


def test_cadastrar_funcionario_com_email_repetido(db, fono):
    criar_cliente(db, email="carlos@exemplo.com")
    with pytest.raises(auth_service.EmailJaCadastrado):
        funcionario_service.cadastrar(db, FuncionarioCadastro(**FORM, especialidade_id=fono.id), AGORA)


def test_cadastrar_funcionario_com_especialidade_inexistente(db):
    with pytest.raises(especialidade_service.EspecialidadeNaoEncontrada):
        funcionario_service.cadastrar(db, FuncionarioCadastro(**FORM, especialidade_id=999), AGORA)
    assert db.query(Usuario).count() == 0


def test_editar_nome_e_especialidade(db, fono):
    psico = criar_especialidade(db, "Psicologia")
    resumo = criar_funcionario(db, fono)
    funcionario_service.atualizar(db, resumo.usuario.id, FuncionarioEdicao(nome="Carlos Psico", especialidade_id=psico.id), AGORA_RN)
    atualizado = funcionario_service.buscar(db, resumo.usuario.id)
    assert (atualizado.usuario.nome, atualizado.especialidade.nome) == ("Carlos Psico", "Psicologia")


def test_rn05_desativa_funcionario_sem_sessoes(db, fono):
    resumo = criar_funcionario(db, fono)
    funcionario_service.desativar(db, resumo.usuario.id, AGORA_RN)
    assert db.get(Funcionario, resumo.usuario.id).ativo is False


@pytest.mark.parametrize("data_hora", [FUTURO, AGORA_RN])
def test_rn05_bloqueia_desativacao_com_sessao_agendada_futura(db, fono, data_hora):
    resumo = criar_funcionario(db, fono)
    criar_plano_com_sessao(db, resumo.usuario.id, fono, StatusSessao.AGENDADA, data_hora=data_hora)

    with pytest.raises(funcionario_service.FuncionarioComSessoesAgendadas):
        funcionario_service.desativar(db, resumo.usuario.id, AGORA_RN)
    assert db.get(Funcionario, resumo.usuario.id).ativo is True


def test_rn05_sessao_agendada_so_no_passado_nao_bloqueia(db, fono):
    resumo = criar_funcionario(db, fono)
    criar_plano_com_sessao(db, resumo.usuario.id, fono, StatusSessao.AGENDADA, data_hora=PASSADO)
    funcionario_service.desativar(db, resumo.usuario.id, AGORA_RN)
    assert db.get(Funcionario, resumo.usuario.id).ativo is False


@pytest.mark.parametrize("status", [StatusSessao.PENDENTE_PAGAMENTO, StatusSessao.REALIZADA])
def test_rn05_so_considera_sessoes_com_status_agendada(db, fono, status):
    resumo = criar_funcionario(db, fono)
    criar_plano_com_sessao(db, resumo.usuario.id, fono, status, data_hora=FUTURO)
    funcionario_service.desativar(db, resumo.usuario.id, AGORA_RN)
    assert db.get(Funcionario, resumo.usuario.id).ativo is False


def test_rn05_nao_considera_sessoes_de_outro_funcionario(db, fono):
    colega = criar_funcionario(db, fono, email="colega@exemplo.com")
    criar_plano_com_sessao(db, colega.usuario.id, fono, StatusSessao.AGENDADA, data_hora=FUTURO)
    resumo = criar_funcionario(db, fono)
    funcionario_service.desativar(db, resumo.usuario.id, AGORA_RN)
    assert db.get(Funcionario, resumo.usuario.id).ativo is False


@pytest.mark.parametrize("status", [StatusSessao.AGENDADA, StatusSessao.PENDENTE_PAGAMENTO])
def test_troca_de_especialidade_bloqueada_com_sessao_futura(db, fono, status):
    psico = criar_especialidade(db, "Psicologia")
    resumo = criar_funcionario(db, fono)
    criar_plano_com_sessao(db, resumo.usuario.id, fono, status, data_hora=FUTURO)

    with pytest.raises(funcionario_service.EspecialidadeComSessoesFuturas):
        dados = FuncionarioEdicao(nome="Carlos Psico", especialidade_id=psico.id)
        funcionario_service.atualizar(db, resumo.usuario.id, dados, AGORA_RN)
    db.refresh(resumo.funcionario)
    db.refresh(resumo.usuario)
    assert (resumo.funcionario.especialidade_id, resumo.usuario.nome) == (fono.id, "Carlos Fono")


@pytest.mark.parametrize("status", [StatusSessao.AGENDADA, StatusSessao.PENDENTE_PAGAMENTO])
def test_troca_de_especialidade_permitida_com_sessoes_so_no_passado(db, fono, status):
    psico = criar_especialidade(db, "Psicologia")
    resumo = criar_funcionario(db, fono)
    criar_plano_com_sessao(db, resumo.usuario.id, fono, status, data_hora=PASSADO)
    dados = FuncionarioEdicao(nome="Carlos Fono", especialidade_id=psico.id)
    funcionario_service.atualizar(db, resumo.usuario.id, dados, AGORA_RN)
    assert db.get(Funcionario, resumo.usuario.id).especialidade_id == psico.id


def test_alterar_so_o_nome_com_sessao_futura_e_permitido(db, fono):
    resumo = criar_funcionario(db, fono)
    criar_plano_com_sessao(db, resumo.usuario.id, fono, StatusSessao.AGENDADA, data_hora=FUTURO)
    dados = FuncionarioEdicao(nome="Carlos Novo", especialidade_id=fono.id)
    funcionario_service.atualizar(db, resumo.usuario.id, dados, AGORA_RN)
    atualizado = funcionario_service.buscar(db, resumo.usuario.id)
    assert (atualizado.usuario.nome, atualizado.funcionario.especialidade_id) == ("Carlos Novo", fono.id)


def test_reativar_funcionario(db, fono):
    resumo = criar_funcionario(db, fono)
    funcionario_service.desativar(db, resumo.usuario.id, AGORA_RN)
    funcionario_service.reativar(db, resumo.usuario.id)
    assert db.get(Funcionario, resumo.usuario.id).ativo is True


# --- Rotas ---------------------------------------------------------------------------------------

def test_lista_mostra_nome_especialidade_e_status(client, db, fono, admin_logado):
    ativo = criar_funcionario(db, fono, email="ativo@exemplo.com", nome="Bruna Ativa")
    inativo = criar_funcionario(db, fono, email="inativo@exemplo.com", nome="Davi Inativo")
    funcionario_service.desativar(db, inativo.usuario.id, AGORA_RN)

    html = client.get("/admin/funcionarios").text
    assert "Bruna Ativa" in html and "Davi Inativo" in html and "Fonoaudiologia" in html
    assert ">Ativo<" in html and ">Inativo<" in html
    assert f"/admin/funcionarios/{ativo.usuario.id}/desativar" in html
    assert f"/admin/funcionarios/{inativo.usuario.id}/reativar" in html


def test_formulario_avisa_quando_nao_ha_especialidade(client, admin_logado):
    html = client.get("/admin/funcionarios/novo").text
    assert "Nenhuma especialidade cadastrada ainda" in html
    assert 'href="/admin/especialidades/nova"' in html
    assert 'name="email"' not in html


def test_cadastrar_funcionario_pela_tela_e_funcionario_consegue_logar(client, db, fono, admin_logado):
    html = client.get("/admin/funcionarios/novo").text
    assert f'<option value="{fono.id}"' in html

    resposta = client.post("/admin/funcionarios/novo", data={**FORM, "especialidade_id": str(fono.id)}, follow_redirects=False)
    assert resposta.status_code == 303
    assert "Carlos Fono" in client.get(resposta.headers["location"]).text

    client.post("/logout")
    logar(client, "carlos@exemplo.com", "provisoria1")


@pytest.mark.parametrize(
    "dados, campo_msg",
    [
        ({**FORM, "especialidade_id": ""}, "Este campo é obrigatório."),
        ({**FORM, "especialidade_id": "999"}, "Selecione uma especialidade da lista."),
        ({**FORM, "especialidade_id": "abc"}, "Verifique este campo."),
        ({**FORM, "email": "invalido"}, "Informe um e-mail válido"),
        ({**FORM, "senha": "curta"}, "pelo menos 8 caracteres"),
    ],
)
def test_cadastro_de_funcionario_com_erro(client, db, fono, admin_logado, dados, campo_msg):
    resposta = client.post("/admin/funcionarios/novo", data=dados)
    assert resposta.status_code == 400
    assert campo_msg in resposta.text
    assert dados["senha"] not in resposta.text
    assert db.query(Funcionario).count() == 0


def test_editar_funcionario_pela_tela(client, db, fono, admin_logado, relogio_fixo):
    psico = criar_especialidade(db, "Psicologia")
    resumo = criar_funcionario(db, fono)
    url = f"/admin/funcionarios/{resumo.usuario.id}/editar"
    assert "func@exemplo.com" in client.get(url).text

    resposta = client.post(url, data={"nome": "Carlos Psico", "especialidade_id": str(psico.id)}, follow_redirects=False)
    assert resposta.status_code == 303
    assert funcionario_service.buscar(db, resumo.usuario.id).especialidade.nome == "Psicologia"


def test_rn05_desativacao_bloqueada_mostra_motivo(client, db, fono, admin_logado, relogio_fixo):
    resumo = criar_funcionario(db, fono)
    criar_plano_com_sessao(db, resumo.usuario.id, fono, StatusSessao.AGENDADA, data_hora=FUTURO)

    resposta = client.post(f"/admin/funcionarios/{resumo.usuario.id}/desativar")
    assert resposta.status_code == 409
    assert "Não é possível desativar Carlos Fono: ele possui sessões futuras agendadas." in resposta.text
    assert "transfira" not in resposta.text.lower()
    db.refresh(resumo.funcionario)
    assert resumo.funcionario.ativo is True


def test_rn05_sessao_so_no_passado_desativa_pela_tela(client, db, fono, admin_logado, relogio_fixo):
    resumo = criar_funcionario(db, fono)
    criar_plano_com_sessao(db, resumo.usuario.id, fono, StatusSessao.AGENDADA, data_hora=PASSADO)
    resposta = client.post(f"/admin/funcionarios/{resumo.usuario.id}/desativar", follow_redirects=False)
    assert resposta.status_code == 303
    db.refresh(resumo.funcionario)
    assert resumo.funcionario.ativo is False


def test_troca_de_especialidade_bloqueada_pela_tela(client, db, fono, admin_logado, relogio_fixo):
    psico = criar_especialidade(db, "Psicologia")
    resumo = criar_funcionario(db, fono)
    criar_plano_com_sessao(db, resumo.usuario.id, fono, StatusSessao.PENDENTE_PAGAMENTO, data_hora=FUTURO)
    url = f"/admin/funcionarios/{resumo.usuario.id}/editar"

    resposta = client.post(url, data={"nome": "Carlos Fono", "especialidade_id": str(psico.id)})
    assert resposta.status_code == 409
    assert (
        "Não é possível alterar a especialidade de Carlos Fono: "
        "ele possui sessões futuras agendadas ou aguardando pagamento."
    ) in resposta.text
    db.refresh(resumo.funcionario)
    assert resumo.funcionario.especialidade_id == fono.id

    resposta = client.post(url, data={"nome": "Carlos Renomeado", "especialidade_id": str(fono.id)}, follow_redirects=False)
    assert resposta.status_code == 303
    assert funcionario_service.buscar(db, resumo.usuario.id).usuario.nome == "Carlos Renomeado"


SESSAO_10H = datetime(2026, 10, 17, 10, 0)


def test_regressao_fuso_sessao_nao_vira_passada_3h_antes(client, db, fono, admin_logado, monkeypatch):
    # 10:01 UTC = 07:01 em Brasília: com o bug antigo, a sessão das 10:00 locais já contava como passada
    agora_utc_fixo = datetime(2026, 10, 17, 10, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(admin_router, "agora_local", lambda: utc_para_local(agora_utc_fixo))
    psico = criar_especialidade(db, "Psicologia")
    resumo = criar_funcionario(db, fono)
    criar_plano_com_sessao(db, resumo.usuario.id, fono, StatusSessao.AGENDADA, data_hora=SESSAO_10H)

    assert client.post(f"/admin/funcionarios/{resumo.usuario.id}/desativar").status_code == 409
    dados = {"nome": "Carlos Fono", "especialidade_id": str(psico.id)}
    assert client.post(f"/admin/funcionarios/{resumo.usuario.id}/editar", data=dados).status_code == 409
    db.refresh(resumo.funcionario)
    assert (resumo.funcionario.ativo, resumo.funcionario.especialidade_id) == (True, fono.id)


@pytest.mark.parametrize(
    "agora, bloqueia",
    [(datetime(2026, 10, 17, 7, 1), True), (SESSAO_10H, True), (datetime(2026, 10, 17, 10, 1), False)],
)
def test_rn05_limite_do_horario_da_sessao(db, fono, agora, bloqueia):
    resumo = criar_funcionario(db, fono)
    criar_plano_com_sessao(db, resumo.usuario.id, fono, StatusSessao.AGENDADA, data_hora=SESSAO_10H)
    if bloqueia:
        with pytest.raises(funcionario_service.FuncionarioComSessoesAgendadas):
            funcionario_service.desativar(db, resumo.usuario.id, agora)
    else:
        funcionario_service.desativar(db, resumo.usuario.id, agora)
    assert db.get(Funcionario, resumo.usuario.id).ativo is bloqueia


def test_desativar_e_reativar_pela_tela(client, db, fono, admin_logado, relogio_fixo):
    resumo = criar_funcionario(db, fono)
    resposta = client.post(f"/admin/funcionarios/{resumo.usuario.id}/desativar", follow_redirects=False)
    assert resposta.status_code == 303
    assert "Funcionário desativado." in client.get(resposta.headers["location"]).text

    client.post(f"/admin/funcionarios/{resumo.usuario.id}/reativar")
    db.refresh(resumo.funcionario)
    assert resumo.funcionario.ativo is True


def test_funcionario_inexistente_retorna_404(client, admin_logado):
    assert client.get("/admin/funcionarios/999/editar").status_code == 404
    assert client.post("/admin/funcionarios/999/desativar").status_code == 404


@pytest.mark.parametrize("perfil", ["cliente", "funcionario"])
def test_rotas_de_funcionarios_bloqueadas_para_quem_nao_e_admin(client, db, fono, perfil, relogio_fixo):
    alvo = criar_funcionario(db, fono, email="alvo@exemplo.com")
    usuario = criar_cliente(db) if perfil == "cliente" else criar_funcionario(db, fono).usuario
    logar(client, usuario.email)

    assert client.get("/admin/funcionarios").status_code == 403
    assert client.get("/admin/funcionarios/novo").status_code == 403
    assert client.post("/admin/funcionarios/novo", data={**FORM, "especialidade_id": str(fono.id)}).status_code == 403
    assert client.post(f"/admin/funcionarios/{alvo.usuario.id}/desativar").status_code == 403
    db.refresh(alvo.funcionario)
    assert alvo.funcionario.ativo is True


def test_rotas_de_admin_exigem_login(client):
    resposta = client.get("/admin/funcionarios", follow_redirects=False)
    assert (resposta.status_code, resposta.headers["location"]) == (303, "/login")
