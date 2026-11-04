from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.core.fuso import utc_para_local
from app.models.avaliacao import Avaliacao
from app.models.especialidade import Especialidade
from app.models.paciente import Paciente
from app.models.plano import Plano
from app.models.sessao import Sessao, StatusSessao
from app.models.usuario import Usuario
from app.services.admin_clientes_service import POR_PAGINA
from app.services.inicio_service import AGUARDANDO_PAGAMENTO, planos_do_cliente
from app.services.plano_service import DURACAO_SESSAO
from app.services.remarcacao_service import motivo_bloqueio

PROXIMAS = "proximas"
ANTERIORES = "anteriores"
ABAS = (PROXIMAS, ANTERIORES)


@dataclass(frozen=True)
class ConsultaCliente:
    id: int
    data_hora: datetime
    fim: datetime
    atendido: str
    profissional: str
    especialidade: str
    status: StatusSessao
    remarcada: bool
    motivo_bloqueio: str | None

    @property
    def pode_remarcar(self) -> bool:
        return self.motivo_bloqueio is None


@dataclass(frozen=True)
class PlanoAguardandoPagamento:
    plano_id: int
    atendido: str
    especialidade: str
    sessoes: int


@dataclass(frozen=True)
class PaginaConsultas:
    consultas: list[ConsultaCliente]
    aba: str
    pagina: int
    total_paginas: int
    total: int


def _consulta(cliente_id: int, aba: str | None, agora: datetime, *colunas, paciente_id: int | None = None):
    condicoes = [Paciente.cliente_id == cliente_id]
    if aba is not None:
        condicoes.append(Sessao.data_hora >= agora if aba == PROXIMAS else Sessao.data_hora < agora)
    if paciente_id is not None:
        condicoes.append(Paciente.id == paciente_id)
    return (
        select(*colunas)
        .select_from(Sessao)
        .join(Plano, Plano.id == Sessao.plano_id)
        .join(Paciente, Paciente.id == Plano.paciente_id)
        .where(*condicoes)
    )


def consultas_do_cliente(db: Session, cliente_id: int, agora: datetime, aba: str = PROXIMAS, pagina: int = 1) -> PaginaConsultas:
    aba = aba if aba in ABAS else PROXIMAS
    pagina = max(pagina, 1)
    ordem = (Sessao.data_hora, Sessao.id) if aba == PROXIMAS else (Sessao.data_hora.desc(), Sessao.id.desc())
    colunas = (
        Sessao.id, Sessao.data_hora, Paciente.nome, Usuario.nome, Especialidade.nome, Sessao.status,
        Sessao.foi_remarcada, func.count().over(),
    )

    def buscar(numero: int):
        return db.execute(
            _consulta(cliente_id, aba, agora, *colunas)
            .join(Especialidade, Especialidade.id == Plano.especialidade_id)
            .join(Usuario, Usuario.id == Plano.funcionario_id)
            .order_by(*ordem)
            .limit(POR_PAGINA)
            .offset((numero - 1) * POR_PAGINA)
        ).all()

    linhas = buscar(pagina)
    if not linhas and pagina > 1:
        total = db.scalar(_consulta(cliente_id, aba, agora, func.count(Sessao.id)))
        pagina = max(1, -(-total // POR_PAGINA))
        linhas = buscar(pagina) if total else []

    total = linhas[0][-1] if linhas else 0
    consultas = [
        ConsultaCliente(
            id_, data_hora, data_hora + DURACAO_SESSAO, atendido, profissional, especialidade, status, remarcada,
            motivo_bloqueio(status, data_hora, agora),
        )
        for id_, data_hora, atendido, profissional, especialidade, status, remarcada, _ in linhas
    ]
    return PaginaConsultas(consultas, aba, pagina, max(1, -(-total // POR_PAGINA)), total)


def planos_aguardando_pagamento(db: Session, cliente_id: int, agora: datetime) -> list[PlanoAguardandoPagamento]:
    return [
        PlanoAguardandoPagamento(r.plano.id, r.paciente, r.especialidade, r.total_sessoes)
        for r in planos_do_cliente(db, cliente_id, agora)
        if r.situacao == AGUARDANDO_PAGAMENTO
    ]


# --- Histórico de um atendido (RF10 / HU13) ------------------------------------------------------

@dataclass(frozen=True)
class ConsultaHistorico:
    id: int
    data_hora: datetime
    fim: datetime
    profissional: str
    especialidade: str
    status: StatusSessao
    remarcada: bool
    avaliacao_id: int | None
    comparecimento: bool | None
    tem_arquivo: bool
    liberado_em: datetime | None
    aguardando_registro: bool

    @property
    def avaliacao_disponivel(self) -> bool:
        return self.avaliacao_id is not None and bool(self.comparecimento) and self.tem_arquivo

    @property
    def nao_compareceu(self) -> bool:
        return self.avaliacao_id is not None and self.comparecimento is False


@dataclass(frozen=True)
class ResumoHistorico:
    realizadas: int = 0
    agendadas: int = 0
    aguardando_pagamento: int = 0
    avaliacoes: int = 0


@dataclass(frozen=True)
class PaginaHistorico:
    consultas: list[ConsultaHistorico]
    pagina: int
    total_paginas: int
    total: int
    resumo: ResumoHistorico


def _contar_no_total(condicao):
    return func.sum(case((condicao, 1), else_=0)).over()


def historico_do_atendido(
    db: Session, cliente_id: int, paciente_id: int, agora: datetime, pagina: int = 1,
) -> PaginaHistorico:
    pagina = max(pagina, 1)
    com_arquivo = Avaliacao.arquivo_url.is_not(None)
    colunas = (
        Sessao.id, Sessao.data_hora, Usuario.nome, Especialidade.nome, Sessao.status, Sessao.foi_remarcada,
        Avaliacao.id, Avaliacao.comparecimento, com_arquivo, Avaliacao.liberado_em,
        func.count().over(),
        _contar_no_total(Sessao.status == StatusSessao.REALIZADA),
        _contar_no_total(Sessao.status == StatusSessao.AGENDADA),
        _contar_no_total(Sessao.status == StatusSessao.PENDENTE_PAGAMENTO),
        _contar_no_total(Avaliacao.comparecimento.is_(True) & com_arquivo),
    )

    def buscar(numero: int):
        # Posse do atendido no WHERE (Paciente.cliente_id): atendido de outro Cliente nunca devolve linha
        return db.execute(
            _consulta(cliente_id, None, agora, *colunas, paciente_id=paciente_id)
            .join(Especialidade, Especialidade.id == Plano.especialidade_id)
            .join(Usuario, Usuario.id == Plano.funcionario_id)
            .outerjoin(Avaliacao, Avaliacao.sessao_id == Sessao.id)
            .order_by(Sessao.data_hora.desc(), Sessao.id.desc())
            .limit(POR_PAGINA)
            .offset((numero - 1) * POR_PAGINA)
        ).all()

    linhas = buscar(pagina)
    if not linhas and pagina > 1:
        total = db.scalar(_consulta(cliente_id, None, agora, func.count(Sessao.id), paciente_id=paciente_id))
        pagina = max(1, -(-total // POR_PAGINA))
        linhas = buscar(pagina) if total else []

    if not linhas:
        return PaginaHistorico([], pagina, 1, 0, ResumoHistorico())
    total, realizadas, agendadas, pendentes, avaliacoes = (int(valor or 0) for valor in linhas[0][-5:])
    consultas = [
        ConsultaHistorico(
            id_, data_hora, data_hora + DURACAO_SESSAO, profissional, especialidade, status, bool(remarcada),
            avaliacao_id, comparecimento, bool(tem_arquivo), utc_para_local(liberado_em) if liberado_em else None,
            status == StatusSessao.AGENDADA and data_hora + DURACAO_SESSAO <= agora,
        )
        for (id_, data_hora, profissional, especialidade, status, remarcada,
             avaliacao_id, comparecimento, tem_arquivo, liberado_em, *_contagens) in linhas
    ]
    resumo = ResumoHistorico(realizadas, agendadas, pendentes, avaliacoes)
    return PaginaHistorico(consultas, pagina, max(1, -(-total // POR_PAGINA)), total, resumo)
