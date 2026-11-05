from dataclasses import dataclass
from datetime import date, datetime, time

from sqlalchemy import and_, case, func, select
from sqlalchemy.orm import Session

from app.models.avaliacao import Avaliacao
from app.models.especialidade import Especialidade
from app.models.paciente import Paciente
from app.models.plano import Plano
from app.models.sessao import Sessao, StatusSessao
from app.services.admin_clientes_service import POR_PAGINA, calcular_idade
from app.services.avaliacao_service import pode_registrar, termino
from app.services.plano_service import DURACAO_SESSAO

# Vínculo com o atendido (mesma leitura da RN03 e do RF08): plano só aguardando pagamento não conta
STATUS_VINCULO = (StatusSessao.AGENDADA, StatusSessao.REALIZADA)


class AtendidoNaoEncontrado(Exception):
    pass


@dataclass(frozen=True)
class AtendidoResumo:
    id: int
    nome: str
    idade: int
    especialidades: str
    realizadas: int
    agendadas_futuras: int
    aguardando_registro: int
    proxima: datetime | None


@dataclass(frozen=True)
class PaginaAtendidos:
    atendidos: list[AtendidoResumo]
    pagina: int
    total_paginas: int
    total: int
    busca: str


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
    especialidade: str
    dias_semana: list[int]
    horario: time
    frequencia_semanal: int
    duracao: str
    primeira: datetime
    ultima: datetime
    total_sessoes: int
    realizadas: int
    observacao: str | None

    @property
    def percentual(self) -> int:
        return round(100 * self.realizadas / self.total_sessoes) if self.total_sessoes else 0


@dataclass(frozen=True)
class SessaoComProfissional:
    id: int
    inicio: datetime
    fim: datetime
    especialidade: str
    status: StatusSessao
    remarcada: bool
    avaliacao_id: int | None
    comparecimento: bool | None
    tem_arquivo: bool
    pode_registrar: bool


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


def _total_paginas(total: int) -> int:
    return max(1, -(-total // POR_PAGINA))


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
) -> PaginaAtendidos:
    busca = (busca or "").strip()
    pagina = max(pagina, 1)
    realizadas, futuras, aguardando, proxima = _contagens(agora)

    def filtrar(consulta):
        if busca:
            consulta = consulta.where(func.lower(Paciente.nome).contains(busca.lower(), autoescape=True))
        return consulta

    def buscar(numero: int):
        return db.execute(
            filtrar(_base(
                funcionario_id,
                Paciente.id, Paciente.nome, Paciente.data_nascimento,
                func.min(Especialidade.nome), func.max(Especialidade.nome),
                realizadas, futuras, aguardando, proxima, func.count().over(),
            ).join(Especialidade, Especialidade.id == Plano.especialidade_id))
            .group_by(Paciente.id, Paciente.nome, Paciente.data_nascimento)
            .order_by(Paciente.nome, Paciente.id)
            .limit(POR_PAGINA)
            .offset((numero - 1) * POR_PAGINA)
        ).all()

    linhas = buscar(pagina)
    if not linhas and pagina > 1:
        total = db.scalar(filtrar(_base(funcionario_id, func.count(func.distinct(Paciente.id)))))
        pagina = _total_paginas(total)
        linhas = buscar(pagina) if total else []

    total = linhas[0][-1] if linhas else 0
    hoje = agora.date()
    atendidos = [
        AtendidoResumo(
            id_, nome, calcular_idade(nascimento, hoje),
            esp_min if esp_min == esp_max else f"{esp_min}, {esp_max}",
            int(n_realizadas or 0), int(n_futuras or 0), int(n_aguardando or 0), prox,
        )
        for id_, nome, nascimento, esp_min, esp_max, n_realizadas, n_futuras, n_aguardando, prox, _ in linhas
    ]
    return PaginaAtendidos(atendidos, pagina, _total_paginas(total), total, busca)


def detalhe_do_atendido(
    db: Session, funcionario_id: int, paciente_id: int, agora: datetime, pagina: int = 1,
) -> DetalheAtendido:
    realizadas, futuras, aguardando, _ = _contagens(agora)
    nao_compareceu = _somar(Avaliacao.comparecimento.is_(False))
    enviadas = _somar(and_(Avaliacao.comparecimento.is_(True), Avaliacao.arquivo_url.is_not(None)))
    cabecalho = db.execute(
        _base(funcionario_id, Paciente.nome, Paciente.data_nascimento, realizadas, futuras, aguardando,
              nao_compareceu, enviadas)
        .outerjoin(Avaliacao, Avaliacao.sessao_id == Sessao.id)
        .where(Paciente.id == paciente_id)
        .group_by(Paciente.id, Paciente.nome, Paciente.data_nascimento)
    ).first()
    if cabecalho is None:
        raise AtendidoNaoEncontrado
    nome, nascimento, *numeros = cabecalho
    resumo = ResumoAtendido(*(int(n or 0) for n in numeros))

    planos = [
        PlanoComProfissional(
            id_, especialidade, list(dias or []), horario, frequencia, duracao, primeira, ultima,
            int(total or 0), int(feitas or 0), (observacao or "").strip() or None,
        )
        for id_, especialidade, dias, horario, frequencia, duracao, observacao, primeira, ultima, total, feitas
        in db.execute(
            _base(
                funcionario_id,
                Plano.id, Especialidade.nome, Plano.dias_semana, Plano.horario, Plano.frequencia_semanal,
                Plano.duracao, Plano.observacao, func.min(Sessao.data_hora), func.max(Sessao.data_hora),
                func.count(Sessao.id), _somar(Sessao.status == StatusSessao.REALIZADA),
            )
            .join(Especialidade, Especialidade.id == Plano.especialidade_id)
            .where(Paciente.id == paciente_id)
            .group_by(Plano.id, Especialidade.nome, Plano.dias_semana, Plano.horario, Plano.frequencia_semanal,
                      Plano.duracao, Plano.observacao)
            .order_by(func.min(Sessao.data_hora).desc(), Plano.id.desc())
        )
    ]

    # O total vem do resumo: a página já sai certa e não há comando extra para página além do fim
    total_paginas = _total_paginas(resumo.total_sessoes)
    pagina = min(max(pagina, 1), total_paginas)
    sessoes = [
        SessaoComProfissional(
            id_, data_hora, termino(data_hora), especialidade, status,
            bool(remarcada), avaliacao_id, comparecimento, bool(tem_arquivo), pode_registrar(status, data_hora, agora),
        )
        for id_, data_hora, especialidade, status, remarcada, avaliacao_id, comparecimento, tem_arquivo
        in db.execute(
            _base(
                funcionario_id,
                Sessao.id, Sessao.data_hora, Especialidade.nome, Sessao.status,
                Sessao.foi_remarcada, Avaliacao.id, Avaliacao.comparecimento, Avaliacao.arquivo_url.is_not(None),
            )
            .join(Especialidade, Especialidade.id == Plano.especialidade_id)
            .outerjoin(Avaliacao, Avaliacao.sessao_id == Sessao.id)
            .where(Paciente.id == paciente_id)
            .order_by(Sessao.data_hora.desc(), Sessao.id.desc())
            .limit(POR_PAGINA)
            .offset((pagina - 1) * POR_PAGINA)
        )
    ]
    return DetalheAtendido(
        paciente_id, nome, calcular_idade(nascimento, agora.date()), resumo, planos, sessoes, pagina, total_paginas,
    )
