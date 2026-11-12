from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from app.core.ids import ler_opcao
from app.core.templating import NOMES_DIAS, descrever_configuracao
from app.models.avaliacao import Avaliacao
from app.models.paciente import Paciente
from app.models.plano import Plano
from app.models.sessao import Sessao, StatusSessao
from app.services.admin_clientes_service import POR_PAGINA, calcular_idade
from app.services.agenda_service import (
    AGUARDANDO_REGISTRO,
    EstadoConsulta,
    atraso_destacado,
    estado_da_consulta,
    ha_quanto_tempo,
)
from app.services.avaliacao_service import pode_registrar, termino
from app.services.inicio_profissional_service import dia_abreviado
from app.services.plano_service import DURACAO_SESSAO

# Vínculo com o atendido (mesma leitura da RN03 e do RF08): plano só aguardando pagamento não conta
STATUS_VINCULO = (StatusSessao.AGENDADA, StatusSessao.REALIZADA)

FILTRO_TODOS = "todos"
FILTRO_PENDENTE = "pendente"
FILTROS = (FILTRO_TODOS, FILTRO_PENDENTE)


class AtendidoNaoEncontrado(Exception):
    pass


@dataclass(frozen=True)
class AtendidoResumo:
    id: int
    nome: str
    idade: int
    realizadas: int
    agendadas_futuras: int
    aguardando_registro: int
    proxima: datetime | None
    proxima_texto: str | None = None


@dataclass(frozen=True)
class PaginaAtendidos:
    atendidos: list[AtendidoResumo]
    pagina: int
    total_paginas: int
    total: int
    busca: str
    filtro: str = FILTRO_TODOS
    total_todos: int = 0
    total_pendentes: int = 0


@dataclass(frozen=True)
class ResumoAtendido:
    realizadas: int
    agendadas_futuras: int
    aguardando_registro: int
    nao_comparecimentos: int
    avaliacoes_enviadas: int

    @property
    def total_sessoes(self) -> int:
        return self.realizadas + self.agendadas_futuras + self.aguardando_registro


@dataclass(frozen=True)
class PlanoComProfissional:
    id: int
    dias_semana: list[int]
    horario: time
    frequencia_semanal: int
    duracao: str
    primeira: datetime
    ultima: datetime
    total_sessoes: int
    realizadas: int
    observacao: str | None
    aguardando_registro: int = 0

    @property
    def percentual(self) -> int:
        return round(100 * self.realizadas / self.total_sessoes) if self.total_sessoes else 0

    @property
    def configuracao(self) -> str:
        return descrever_configuracao(self.duracao, self.frequencia_semanal, self.dias_semana, self.horario)


@dataclass(frozen=True)
class SessaoComProfissional:
    id: int
    inicio: datetime
    fim: datetime
    status: StatusSessao
    remarcada: bool
    avaliacao_id: int | None
    comparecimento: bool | None
    tem_arquivo: bool
    pode_registrar: bool
    estado: EstadoConsulta | None = None
    atraso: str | None = None
    atraso_destacado: bool = False


@dataclass(frozen=True)
class DetalheAtendido:
    id: int
    nome: str
    idade: int
    resumo: ResumoAtendido
    planos: list[PlanoComProfissional]
    sessoes: list[SessaoComProfissional]
    pagina: int
    total_paginas: int
    proxima: datetime | None = None
    proxima_texto: str | None = None


# --- Funções puras (RNF14: agora sempre por parâmetro) -------------------------------------------

def quando_relativo(momento: datetime, agora: datetime) -> str:
    hoje = agora.date()
    if momento.date() == hoje:
        dia = "hoje"
    elif momento.date() == hoje + timedelta(days=1):
        dia = "amanhã"
    else:
        dia = dia_abreviado(momento.date(), hoje)
    return f"{dia}, {momento:%H:%M}"


def _total_paginas(total: int) -> int:
    return max(1, -(-total // POR_PAGINA))


# --- Consultas ao banco ---------------------------------------------------------------------------

def _somar(condicao):
    return func.sum(case((condicao, 1), else_=0))


def _contagens(agora: datetime):
    # "Encerrada" = término (início + DURACAO_SESSAO) já passou, a mesma condição de elegibilidade do RF09
    inicio_das_encerradas = agora - DURACAO_SESSAO
    agendada = Sessao.status == StatusSessao.AGENDADA
    return (
        _somar(Sessao.status == StatusSessao.REALIZADA),
        _somar(and_(agendada, Sessao.data_hora > inicio_das_encerradas)),
        _somar(and_(agendada, Sessao.data_hora <= inicio_das_encerradas)),
        func.min(case((and_(agendada, Sessao.data_hora > inicio_das_encerradas), Sessao.data_hora))),
    )


def _base(funcionario_id: int, *colunas):
    return (
        select(*colunas)
        .select_from(Sessao)
        .join(Plano, Plano.id == Sessao.plano_id)
        .join(Paciente, Paciente.id == Plano.paciente_id)
        .where(Plano.funcionario_id == funcionario_id, Sessao.status.in_(STATUS_VINCULO))
    )


def atendidos_do_profissional(
    db: Session, funcionario_id: int, agora: datetime, pagina: int = 1, busca: str | None = None,
    filtro: str | None = FILTRO_TODOS,
) -> PaginaAtendidos:
    busca = (busca or "").strip()
    filtro = ler_opcao(filtro, FILTROS, FILTRO_TODOS)
    realizadas, futuras, aguardando, proxima = _contagens(agora)

    def filtrar(consulta):
        if busca:
            consulta = consulta.where(func.lower(Paciente.nome).contains(busca.lower(), autoescape=True))
        return consulta

    # Contagens dos dois chips sobre TODOS os vinculados que combinam com a busca, não só sobre a página
    grupos = filtrar(_base(funcionario_id, Paciente.id, aguardando.label("aguardando"))).group_by(Paciente.id).subquery()
    total_todos, total_pendentes = db.execute(
        select(func.count(), func.coalesce(func.sum(case((grupos.c.aguardando > 0, 1), else_=0)), 0)).select_from(grupos)
    ).one()
    total = int(total_pendentes if filtro == FILTRO_PENDENTE else total_todos)
    total_paginas = _total_paginas(total)
    pagina = min(max(pagina, 1), total_paginas)

    linhas = []
    if total:
        consulta = (
            filtrar(_base(
                funcionario_id,
                Paciente.id, Paciente.nome, Paciente.data_nascimento, realizadas, futuras, aguardando, proxima,
            ))
            .group_by(Paciente.id, Paciente.nome, Paciente.data_nascimento)
        )
        if filtro == FILTRO_PENDENTE:
            consulta = consulta.having(aguardando > 0)
        linhas = db.execute(
            consulta.order_by(Paciente.nome, Paciente.id).limit(POR_PAGINA).offset((pagina - 1) * POR_PAGINA)
        ).all()

    hoje = agora.date()
    atendidos = [
        AtendidoResumo(
            id_, nome, calcular_idade(nascimento, hoje),
            int(n_realizadas or 0), int(n_futuras or 0), int(n_aguardando or 0), prox,
            quando_relativo(prox, agora) if prox else None,
        )
        for id_, nome, nascimento, n_realizadas, n_futuras, n_aguardando, prox in linhas
    ]
    return PaginaAtendidos(
        atendidos, pagina, total_paginas, total, busca, filtro, int(total_todos), int(total_pendentes or 0),
    )


def _com_estado(sessao: SessaoComProfissional, agora: datetime) -> SessaoComProfissional:
    estado = estado_da_consulta(sessao, agora)
    if estado != AGUARDANDO_REGISTRO:
        return replace(sessao, estado=estado)
    return replace(
        sessao, estado=estado, atraso=ha_quanto_tempo(sessao.inicio, agora),
        atraso_destacado=atraso_destacado(sessao.inicio, agora),
    )


def detalhe_do_atendido(
    db: Session, funcionario_id: int, paciente_id: int, agora: datetime, pagina: int = 1,
) -> DetalheAtendido:
    realizadas, futuras, aguardando, proxima = _contagens(agora)
    nao_compareceu = _somar(Avaliacao.comparecimento.is_(False))
    enviadas = _somar(and_(Avaliacao.comparecimento.is_(True), Avaliacao.arquivo_url.is_not(None)))
    cabecalho = db.execute(
        _base(funcionario_id, Paciente.nome, Paciente.data_nascimento, realizadas, futuras, aguardando,
              nao_compareceu, enviadas, proxima)
        .outerjoin(Avaliacao, Avaliacao.sessao_id == Sessao.id)
        .where(Paciente.id == paciente_id)
        .group_by(Paciente.id, Paciente.nome, Paciente.data_nascimento)
    ).first()
    if cabecalho is None:
        raise AtendidoNaoEncontrado
    nome, nascimento, *numeros, prox = cabecalho
    resumo = ResumoAtendido(*(int(n or 0) for n in numeros))

    planos = [
        PlanoComProfissional(
            id_, list(dias or []), horario, frequencia, duracao, primeira, ultima,
            int(total or 0), int(feitas or 0), (observacao or "").strip() or None, int(pendentes or 0),
        )
        for id_, dias, horario, frequencia, duracao, observacao, primeira, ultima, total, feitas, pendentes
        in db.execute(
            _base(
                funcionario_id,
                Plano.id, Plano.dias_semana, Plano.horario, Plano.frequencia_semanal, Plano.duracao,
                Plano.observacao, func.min(Sessao.data_hora), func.max(Sessao.data_hora),
                func.count(Sessao.id), realizadas, aguardando,
            )
            .where(Paciente.id == paciente_id)
            .group_by(Plano.id, Plano.dias_semana, Plano.horario, Plano.frequencia_semanal, Plano.duracao,
                      Plano.observacao)
            .order_by(func.min(Sessao.data_hora).desc(), Plano.id.desc())
        )
    ]

    # Só as anteriores (realizadas) são paginadas, e o total vem do resumo: nada de comando extra para a página
    total_paginas = _total_paginas(resumo.realizadas)
    pagina = min(max(pagina, 1), total_paginas)
    primeira_da_pagina = (pagina - 1) * POR_PAGINA + 1
    sub = (
        _base(
            funcionario_id,
            Sessao.id.label("sessao_id"), Sessao.data_hora, Sessao.status, Sessao.foi_remarcada,
            Avaliacao.id.label("avaliacao_id"), Avaliacao.comparecimento,
            Avaliacao.arquivo_url.is_not(None).label("tem_arquivo"),
            func.row_number().over(
                partition_by=Sessao.status, order_by=(Sessao.data_hora.desc(), Sessao.id.desc()),
            ).label("ordem"),
        )
        .outerjoin(Avaliacao, Avaliacao.sessao_id == Sessao.id)
        .where(Paciente.id == paciente_id)
        .subquery()
    )
    sessoes = [
        _com_estado(SessaoComProfissional(
            id_, data_hora, termino(data_hora), status, bool(remarcada), avaliacao_id, comparecimento,
            bool(tem_arquivo), pode_registrar(status, data_hora, agora),
        ), agora)
        for id_, data_hora, status, remarcada, avaliacao_id, comparecimento, tem_arquivo in db.execute(
            select(sub.c.sessao_id, sub.c.data_hora, sub.c.status, sub.c.foi_remarcada, sub.c.avaliacao_id,
                   sub.c.comparecimento, sub.c.tem_arquivo)
            .where(or_(
                sub.c.status != StatusSessao.REALIZADA,
                sub.c.ordem.between(primeira_da_pagina, primeira_da_pagina + POR_PAGINA - 1),
            ))
            .order_by(sub.c.data_hora, sub.c.sessao_id)
        )
    ]
    return DetalheAtendido(
        paciente_id, nome, calcular_idade(nascimento, agora.date()), resumo, planos, sessoes, pagina, total_paginas,
        prox, quando_relativo(prox, agora) if prox else None,
    )
