from datetime import datetime

from app.models.funcionario import Funcionario
from app.models.sessao import StatusSessao
from app.services import auth_service, funcionario_service
from tests.fabricas import criar_plano, logar


def test_rf11_admin_cadastra_profissional_e_desativacao_bloqueada_responde_409(client, db, admin_ana, fono, pedro):
    logar(client, admin_ana.email)

    # 1. Cadastro real do profissional pelo formulário do Admin
    dados = {"nome": "Rita Fono", "email": "rita@exemplo.com", "especialidade_id": str(fono.id), "senha": "senha-da-rita"}
    resposta = client.post("/admin/funcionarios/novo", data=dados, follow_redirects=False)
    assert (resposta.status_code, resposta.headers["location"]) == (303, "/admin/funcionarios?ok=cadastrado")
    rita = funcionario_service.buscar(db, auth_service.buscar_por_email(db, "rita@exemplo.com").id)

    # 2. Com uma consulta futura agendada, a desativação é recusada (RN05)
    criar_plano(db, pedro, rita, [(datetime(2026, 8, 10, 14, 0), StatusSessao.AGENDADA)])
    resposta = client.post(f"/admin/funcionarios/{rita.usuario.id}/desativar", follow_redirects=False)

    assert resposta.status_code == 409 and "Rita Fono" in resposta.text
    db.expire_all()
    assert db.get(Funcionario, rita.usuario.id).ativo is True
