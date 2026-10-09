from datetime import date, datetime, time, timezone
from decimal import Decimal

from app.models.especialidade import Especialidade
from app.models.paciente import Paciente
from app.models.plano import Plano, StatusPlano
from app.models.sessao import Sessao
from app.schemas.funcionario import FuncionarioCadastro
from app.schemas.usuario import AdminCadastro, ClienteCadastro
from app.services import auth_service, funcionario_service

SENHA = "senha123"
AGORA = datetime(2026, 3, 10, 14, 0, tzinfo=timezone.utc)


def criar_cliente(db, email="cliente@exemplo.com", nome="Maria Souza"):
    dados = ClienteCadastro(nome=nome, email=email, telefone="(54) 99999-1234", senha=SENHA)
    return auth_service.cadastrar_cliente(db, dados, AGORA)


def criar_admin(db, email="admin@exemplo.com"):
    return auth_service.cadastrar_admin(db, AdminCadastro(nome="Ana Admin", email=email, senha=SENHA), AGORA)


def criar_especialidade(db, nome="Fonoaudiologia", valor="150.00"):
    especialidade = Especialidade(nome=nome, valor_sessao=Decimal(valor))
    db.add(especialidade)
    db.commit()
    return especialidade


def criar_funcionario(db, especialidade, email="func@exemplo.com", nome="Carlos Fono"):
    dados = FuncionarioCadastro(nome=nome, email=email, especialidade_id=especialidade.id, senha=SENHA)
    return funcionario_service.cadastrar(db, dados, AGORA)


def criar_paciente(db, cliente_id, nome="Pedro Souza", nascimento=date(2018, 5, 3)):
    paciente = Paciente(nome=nome, data_nascimento=nascimento, cliente_id=cliente_id)
    db.add(paciente)
    db.commit()
    return paciente


# Plano e sessões fictícios inseridos direto no banco: o fluxo real de contratação é das Semanas 3/4
def criar_plano_com_sessao(
    db, funcionario_id, especialidade, status_sessao, valor_total="1200.00", data_hora=datetime(2026, 3, 17, 14, 0),
    criado_em=None, email_cliente=None,
):
    cliente = criar_cliente(db, email=email_cliente or f"resp{funcionario_id}-{status_sessao.value}@exemplo.com")
    paciente = criar_paciente(db, cliente.id)
    plano = Plano(
        paciente_id=paciente.id,
        funcionario_id=funcionario_id,
        especialidade_id=especialidade.id,
        dias_semana=[1],
        horario=time(14, 0),
        frequencia_semanal=1,
        duracao="2 meses",
        valor_total=Decimal(valor_total),
        data_inicio=date(2026, 3, 17),
        data_fim=date(2026, 5, 12),
    )
    if criado_em is not None:
        plano.criado_em = criado_em  # UTC sem fuso, como o default da coluna
    db.add(plano)
    db.flush()
    db.add(Sessao(plano_id=plano.id, data_hora=data_hora, status=status_sessao))
    db.commit()
    return plano


def criar_plano(
    db, paciente, funcionario, sessoes, status_plano=StatusPlano.ATIVO, criado_em=datetime(2026, 3, 10, 12, 0),
    valor_total="600.00",
):
    datas = [data_hora for data_hora, _ in sessoes] or [datetime(2026, 3, 17, 14, 0)]
    plano = Plano(
        paciente_id=paciente.id,
        funcionario_id=funcionario.usuario.id,
        especialidade_id=funcionario.especialidade.id,
        dias_semana=sorted({d.weekday() for d in datas}),
        horario=min(datas).time(),
        frequencia_semanal=len({d.weekday() for d in datas}),
        duracao="mensal",
        valor_total=Decimal(valor_total),
        data_inicio=min(datas).date(),
        data_fim=max(datas).date(),
        status=status_plano,
        criado_em=criado_em,  # UTC sem fuso, como o default da coluna
    )
    db.add(plano)
    db.flush()
    db.add_all(Sessao(plano_id=plano.id, data_hora=data_hora, status=status) for data_hora, status in sessoes)
    db.commit()
    return plano


def logar(client, email, senha=SENHA):
    resposta = client.post("/login", data={"email": email, "senha": senha}, follow_redirects=False)
    assert resposta.status_code == 303, "login falhou no setup do teste"
