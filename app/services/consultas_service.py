from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.core.fuso import utc_para_local
from app.core.templating import NOMES_MESES
from app.models.avaliacao import Avaliacao
from app.models.especialidade import Especialidade
from app.models.paciente import Paciente
from app.models.pagamento import Pagamento
from app.models.plano import Plano
from app.models.sessao import Sessao, StatusSessao
from app.models.usuario import Usuario
from app.services.admin_clientes_service import POR_PAGINA
from app.services.contratacao_service import TextoPrazo, texto_prazo_pagamento
from app.services.inicio_cliente_service import PagamentoPendente, pagamentos_pendentes, quando_em_dias
from app.services.inicio_service import ResumoPlano, planos_do_cliente
from app.services.plano_service import DURACAO_SESSAO
from app.services.remarcacao_service import motivo_bloqueio

PROXIMAS = "proximas"
ANTERIORES = "anteriores"
ABAS = (PROXIMAS, ANTERIORES)

REALIZADA = "Realizada"
NAO_COMPARECEU = "Não compareceu"
AGUARDANDO_REGISTRO = "Aguardando registro do profissional"
EM_ANDAMENTO = "Em andamento"


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
    quando: str | None = None
    estado: str | None = None

    @property
    def pode_remarcar(self) -> bool:
        return self.motivo_bloqueio is None


@dataclass(frozen=True)
class MesDeConsultas:
    titulo: str
    consultas: list[ConsultaCliente]


@dataclass(frozen=True)
class PaginaConsultas:
    consultas: list[ConsultaCliente]
    aba: str
    pagina: int
    total_paginas: int
    total: int
    total_proximas: int = 0
    total_anteriores: int = 0

    @property
    def meses(self) -> list[MesDeConsultas]:
        return agrupar_por_mes(self.consultas)


# --- Funções puras (RNF14: `agora` por parâmetro) --------------------------------------------------

def titulo_mes(dia: date) -> str:
    return f"{NOMES_MESES[dia.month - 1].capitalize()} de {dia.year}"


def agrupar_por_mes(consultas: list[ConsultaCliente]) -> list[MesDeConsultas]:
    grupos: list[MesDeConsultas] = []
    for consulta in consultas:
        titulo = titulo_mes(consulta.data_hora.date())
        if not grupos or grupos[-1].titulo != titulo:
            grupos.append(MesDeConsultas(titulo, []))
        grupos[-1].consultas.append(consulta)
    return grupos


def estado_anterior(status: StatusSessao, comparecimento: bool | None, fim: datetime, agora: datetime) -> str:
    if status == StatusSessao.REALIZADA:
        return NAO_COMPARECEU if comparecimento is False else REALIZADA
    return AGUARDANDO_REGISTRO if fim <= agora else EM_ANDAMENTO


# --- Consultas do Cliente (RF07) --------------------------------------------------------------------

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


def _sem_pendentes(consulta):
    return consulta.where(Sessao.status != StatusSessao.PENDENTE_PAGAMENTO)


def _total_da_aba(cliente_id: int, aba: str, agora: datetime, paciente_id: int | None):
    return _sem_pendentes(_consulta(cliente_id, aba, agora, func.count(Sessao.id), paciente_id=paciente_id)).scalar_subquery()


def consultas_do_cliente(
    db: Session, cliente_id: int, agora: datetime, aba: str = PROXIMAS, pagina: int = 1, paciente_id: int | None = None,
) -> PaginaConsultas:
    aba = aba if aba in ABAS else PROXIMAS
    pagina = max(pagina, 1)
    ordem = (Sessao.data_hora, Sessao.id) if aba == PROXIMAS else (Sessao.data_hora.desc(), Sessao.id.desc())
    colunas = (
        Sessao.id, Sessao.data_hora, Paciente.nome, Usuario.nome, Especialidade.nome, Sessao.status,
        Sessao.foi_remarcada, Avaliacao.comparecimento,
        _total_da_aba(cliente_id, PROXIMAS, agora, paciente_id), _total_da_aba(cliente_id, ANTERIORES, agora, paciente_id),
    )

    def buscar(numero: int):
        # As contagens das duas abas vêm em subconsultas do mesmo SELECT: a lista continua com 1 comando SQL
        return db.execute(
            _sem_pendentes(_consulta(cliente_id, aba, agora, *colunas, paciente_id=paciente_id))
            .join(Especialidade, Especialidade.id == Plano.especialidade_id)
            .join(Usuario, Usuario.id == Plano.funcionario_id)
            .outerjoin(Avaliacao, Avaliacao.sessao_id == Sessao.id)
            .order_by(*ordem)
            .limit(POR_PAGINA)
            .offset((numero - 1) * POR_PAGINA)
        ).all()

    linhas = buscar(pagina)
    if linhas:
        proximas, anteriores = linhas[0][-2:]
    else:
        proximas, anteriores = db.execute(
            select(_total_da_aba(cliente_id, PROXIMAS, agora, paciente_id), _total_da_aba(cliente_id, ANTERIORES, agora, paciente_id))
        ).one()
    total = proximas if aba == PROXIMAS else anteriores
    if not linhas and total:
        pagina = max(1, -(-total // POR_PAGINA))
        linhas = buscar(pagina)

    consultas = []
    for id_, data_hora, atendido, profissional, especialidade, status, remarcada, comparecimento, *_ in linhas:
        fim = data_hora + DURACAO_SESSAO
        consultas.append(ConsultaCliente(
            id_, data_hora, fim, atendido, profissional, especialidade, status, bool(remarcada),
            motivo_bloqueio(status, data_hora, agora),
            quando_em_dias(data_hora, agora) if aba == PROXIMAS else None,
            estado_anterior(status, comparecimento, fim, agora) if aba == ANTERIORES else None,
        ))
    return PaginaConsultas(consultas, aba, pagina, max(1, -(-total // POR_PAGINA)), total, proximas, anteriores)


def planos_do_filtro(db: Session, cliente_id: int, agora: datetime, paciente_id: int | None = None) -> list[ResumoPlano]:
    return [r for r in planos_do_cliente(db, cliente_id, agora) if paciente_id is None or r.plano.paciente_id == paciente_id]


def aguardando_pagamento(planos: list[ResumoPlano], agora: datetime) -> list[PagamentoPendente]:
    return pagamentos_pendentes(planos, agora)  # um grupo por plano, menor prazo primeiro


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
    futuras: int = 0

    @property
    def primeira(self) -> int:
        return (self.pagina - 1) * POR_PAGINA + 1 if self.consultas else 0

    @property
    def ultima(self) -> int:
        return (self.pagina - 1) * POR_PAGINA + len(self.consultas)

    @property
    def indice_hoje(self) -> int | None:
        return posicao_do_divisor(self.futuras, self.total, self.pagina, len(self.consultas))


@dataclass(frozen=True)
class PendenciaPagamento:
    plano_id: int
    sessoes: int
    prazo: TextoPrazo


def posicao_do_divisor(futuras: int, total: int, pagina: int, na_pagina: int) -> int | None:
    # Ordem decrescente: as `futuras` ocupam as posições 0..futuras-1; o "Hoje" fica antes da posição `futuras`
    if not 0 < futuras < total:
        return None
    inicio = (pagina - 1) * POR_PAGINA
    return futuras - inicio if inicio <= futuras < inicio + na_pagina else None


def escolher_pendencia(linhas, agora: datetime) -> PendenciaPagamento | None:
    validas = []
    for plano_id, criado_em, data_inicio, forma, sessoes in linhas:
        prazo = texto_prazo_pagamento(criado_em, data_inicio, forma, agora)
        if prazo.prazo >= agora:  # RN09: prazo vencido não pede pagamento
            validas.append((prazo, plano_id, sessoes))
    if not validas:
        return None
    prazo, plano_id, _ = min(validas, key=lambda v: (v[0].prazo, v[1]))
    return PendenciaPagamento(plano_id, sum(v[2] for v in validas), prazo)


def pendencia_do_atendido(db: Session, cliente_id: int, paciente_id: int, agora: datetime) -> PendenciaPagamento | None:
    linhas = db.execute(
        select(Plano.id, Plano.criado_em, Plano.data_inicio, Pagamento.forma_pagamento, func.count(Sessao.id))
        .join(Paciente, Paciente.id == Plano.paciente_id)
        .join(Sessao, (Sessao.plano_id == Plano.id) & (Sessao.status == StatusSessao.PENDENTE_PAGAMENTO))
        .outerjoin(Pagamento, Pagamento.plano_id == Plano.id)
        .where(Plano.paciente_id == paciente_id, Paciente.cliente_id == cliente_id)
        .group_by(Plano.id, Plano.criado_em, Plano.data_inicio, Pagamento.forma_pagamento)
    ).all()
    return escolher_pendencia(linhas, agora)


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
        _contar_no_total(Sessao.data_hora >= agora),
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
    total, realizadas, agendadas, pendentes, avaliacoes, futuras = (int(valor or 0) for valor in linhas[0][-6:])
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
    return PaginaHistorico(consultas, pagina, max(1, -(-total // POR_PAGINA)), total, resumo, futuras)
