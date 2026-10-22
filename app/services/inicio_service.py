from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.templating import NOMES_DIAS
from app.models.especialidade import Especialidade
from app.models.funcionario import Funcionario
from app.models.paciente import Paciente
from app.models.pagamento import FormaPagamento, Pagamento
from app.models.plano import Plano
from app.models.sessao import Sessao, StatusSessao
from app.models.usuario import Usuario
from app.services.plano_service import ocupa_horario, plano_encerrado, prazo_pagamento

ATIVO = "ativo"
AGUARDANDO_PAGAMENTO = "aguardando_pagamento"
ENCERRADO = "encerrado"
ORDEM_SITUACAO = {AGUARDANDO_PAGAMENTO: 0, ATIVO: 1, ENCERRADO: 2}


@dataclass(frozen=True)
class ProximaSessao:
    data_hora: datetime
    paciente: str
    especialidade: str
    profissional: str


@dataclass(frozen=True)
class ResumoPlano:
    plano: Plano
    paciente: str
    especialidade: str
    profissional: str
    situacao: str
    total_sessoes: int
    realizadas: int
    tempo_restante: timedelta | None
    prazo_pagamento: datetime | None = None
    boleto: bool = False


def saudacao(agora: datetime) -> str:
    if 5 <= agora.hour < 12:
        return "Bom dia"
    if 12 <= agora.hour < 18:
        return "Boa tarde"
    return "Boa noite"


def rotulo_dia(dia: date, hoje: date) -> str | None:
    if dia == hoje:
        return "hoje"
    if dia == hoje + timedelta(days=1):
        return "amanhã"
    return None


def quando_sessao(data_hora: datetime, agora: datetime) -> str:
    dia = NOMES_DIAS[data_hora.weekday()]
    data = data_hora.strftime("%d/%m") if data_hora.year == agora.year else data_hora.strftime("%d/%m/%Y")
    rotulo = rotulo_dia(data_hora.date(), agora.date())
    quando = f"{rotulo.capitalize()}, {dia.lower()} {data}" if rotulo else f"{dia} {data}"
    return f"{quando}, às {data_hora.strftime('%H:%M')}"


def _consulta_planos_do_cliente(*colunas):
    profissional = Usuario.nome.label("profissional")
    return (
        select(*colunas, Paciente.nome, Especialidade.nome, profissional)
        .select_from(Plano)
        .join(Paciente, Paciente.id == Plano.paciente_id)
        .join(Especialidade, Especialidade.id == Plano.especialidade_id)
        .join(Usuario, Usuario.id == Plano.funcionario_id)
    )


def proxima_sessao(db: Session, cliente_id: int, agora: datetime) -> ProximaSessao | None:
    consulta = (
        _consulta_planos_do_cliente(Sessao.data_hora)
        .join(Sessao, Sessao.plano_id == Plano.id)
        .where(Paciente.cliente_id == cliente_id)
        .where(Sessao.status == StatusSessao.AGENDADA)
        .where(Sessao.data_hora >= agora)
        .order_by(Sessao.data_hora)
        .limit(1)
    )
    linha = db.execute(consulta).first()
    return ProximaSessao(*linha) if linha else None


def situacao_plano(
    data_fim: date,
    status_sessoes: Counter,
    plano_criado_em_utc: datetime,
    agora: datetime,
    data_primeira_sessao: date | None = None,
    forma: FormaPagamento | None = None,
) -> str | None:
    if status_sessoes[StatusSessao.PENDENTE_PAGAMENTO]:
        if not ocupa_horario(StatusSessao.PENDENTE_PAGAMENTO, plano_criado_em_utc, agora, data_primeira_sessao, forma):
            return None  # RN09
        return AGUARDANDO_PAGAMENTO
    if plano_encerrado(data_fim, False, agora):
        return ENCERRADO
    return ATIVO


def tempo_restante_pagamento(
    plano_criado_em_utc: datetime,
    agora: datetime,
    data_primeira_sessao: date | None = None,
    forma: FormaPagamento | None = None,
) -> timedelta:
    return max(prazo_pagamento(plano_criado_em_utc, data_primeira_sessao, forma) - agora, timedelta(0))


def planos_do_cliente(db: Session, cliente_id: int, agora: datetime) -> list[ResumoPlano]:
    linhas = db.execute(_consulta_planos_do_cliente(Plano).where(Paciente.cliente_id == cliente_id)).all()
    if not linhas:
        return []

    contagens: dict[int, Counter] = {plano.id: Counter() for plano, *_ in linhas}
    consulta_sessoes = (
        select(Sessao.plano_id, Sessao.status, func.count())
        .where(Sessao.plano_id.in_(contagens))
        .group_by(Sessao.plano_id, Sessao.status)
    )
    for plano_id, status, quantidade in db.execute(consulta_sessoes):
        contagens[plano_id][status] = quantidade
    formas = dict(db.execute(
        select(Pagamento.plano_id, Pagamento.forma_pagamento).where(Pagamento.plano_id.in_(contagens))
    ).all())

    resumos = []
    for plano, paciente, especialidade, profissional in linhas:
        status_sessoes = contagens[plano.id]
        forma = formas.get(plano.id)
        situacao = situacao_plano(plano.data_fim, status_sessoes, plano.criado_em, agora, plano.data_inicio, forma)
        if situacao is None:
            continue
        aguardando = situacao == AGUARDANDO_PAGAMENTO
        restante = tempo_restante_pagamento(plano.criado_em, agora, plano.data_inicio, forma) if aguardando else None
        prazo = prazo_pagamento(plano.criado_em, plano.data_inicio, forma) if aguardando else None
        resumos.append(ResumoPlano(
            plano, paciente, especialidade, profissional, situacao,
            total_sessoes=status_sessoes.total(),
            realizadas=status_sessoes[StatusSessao.REALIZADA],
            tempo_restante=restante,
            prazo_pagamento=prazo,
            boleto=forma == FormaPagamento.BOLETO,
        ))
    return sorted(resumos, key=lambda r: (ORDEM_SITUACAO[r.situacao], r.plano.data_inicio, r.plano.id))


def nome_especialidade_do_funcionario(db: Session, funcionario_id: int) -> str | None:
    return db.scalar(
        select(Especialidade.nome)
        .join(Funcionario, Funcionario.especialidade_id == Especialidade.id)
        .where(Funcionario.usuario_id == funcionario_id)
    )


def contar_funcionarios_ativos(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(Funcionario).where(Funcionario.ativo.is_(True)))


def contar_especialidades(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(Especialidade))
