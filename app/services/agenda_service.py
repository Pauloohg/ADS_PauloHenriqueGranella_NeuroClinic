from dataclasses import dataclass
from typing import ClassVar
from datetime import date, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.especialidade import Especialidade
from app.models.paciente import Paciente
from app.models.plano import Plano
from app.models.sessao import Sessao, StatusSessao
from app.services.admin_clientes_service import calcular_idade
from app.services.plano_service import DURACAO_SESSAO, eh_feriado

DIA = "dia"
SEMANA = "semana"
PERIODO = "periodo"
VISOES = (DIA, SEMANA, PERIODO)

LIMITE_PERIODO_DIAS = 92
# Guarda contra datas absurdas digitadas na URL: a navegação soma dias e estouraria date.max
DATA_MINIMA = date(2000, 1, 1)
DATA_MAXIMA = date(2100, 12, 31)

STATUS_VISIVEIS = (StatusSessao.AGENDADA, StatusSessao.REALIZADA)

MSG_DATA_INVALIDA = "Informe uma data válida."
MSG_PERIODO_INCOMPLETO = "Informe a data inicial e a data final do período."
MSG_PERIODO_INVERTIDO = "A data final não pode ser anterior à data inicial."
MSG_PERIODO_LONGO = f"O período pode ter no máximo {LIMITE_PERIODO_DIAS} dias. Escolha um intervalo menor."


class PeriodoInvalido(Exception):
    def __init__(self, mensagem: str):
        super().__init__(mensagem)
        self.mensagem = mensagem


@dataclass(frozen=True)
class Visao:
    tipo: str
    inicio: date
    fim: date

    @property
    def dias(self) -> int:
        return (self.fim - self.inicio).days + 1


@dataclass(frozen=True)
class ConsultaAgenda:
    inicio: datetime
    fim: datetime
    atendido: str
    idade: int
    especialidade: str
    observacao: str | None
    status: StatusSessao
    remarcada: bool


@dataclass(frozen=True)
class DiaAgenda:
    data: date
    consultas: list[ConsultaAgenda]
    feriado: bool
    grupo: ClassVar[bool] = False


@dataclass(frozen=True)
class DiasSemConsulta:
    inicio: date
    fim: date
    grupo: ClassVar[bool] = True


def interpretar_data(texto: str | None) -> date:
    try:
        valor = date.fromisoformat((texto or "").strip())
    except ValueError:
        raise PeriodoInvalido(MSG_DATA_INVALIDA) from None
    if not DATA_MINIMA <= valor <= DATA_MAXIMA:
        raise PeriodoInvalido(MSG_DATA_INVALIDA)
    return valor


def intervalo_da_visao(tipo: str, referencia: date, inicio: date | None = None, fim: date | None = None) -> Visao:
    if tipo == DIA:
        return Visao(DIA, referencia, referencia)
    if tipo == PERIODO:
        if inicio is None or fim is None:
            raise PeriodoInvalido(MSG_PERIODO_INCOMPLETO)
        if fim < inicio:
            raise PeriodoInvalido(MSG_PERIODO_INVERTIDO)
        if (fim - inicio).days + 1 > LIMITE_PERIODO_DIAS:
            raise PeriodoInvalido(MSG_PERIODO_LONGO)
        return Visao(PERIODO, inicio, fim)
    segunda = referencia - timedelta(days=referencia.weekday())
    return Visao(SEMANA, segunda, segunda + timedelta(days=4))


def montar_visao(
    tipo: str | None, data: str | None, inicio: str | None, fim: str | None, hoje: date,
) -> tuple[Visao, str | None]:
    tipo = tipo if tipo in VISOES else SEMANA
    try:
        if tipo == PERIODO:
            return intervalo_da_visao(
                PERIODO, hoje,
                interpretar_data(inicio) if inicio else None,
                interpretar_data(fim) if fim else None,
            ), None
        return intervalo_da_visao(tipo, interpretar_data(data) if data else hoje), None
    except PeriodoInvalido as erro:
        return intervalo_da_visao(SEMANA, hoje), erro.mensagem


def _proximo_dia_util(dia: date, passo: int) -> date:
    dia += timedelta(days=passo)
    while dia.weekday() >= 5:
        dia += timedelta(days=passo)
    return dia


def deslocar_visao(visao: Visao, sentido: int) -> Visao:
    if visao.tipo == DIA:
        dia = _proximo_dia_util(visao.inicio, sentido)
        return Visao(DIA, dia, dia)
    passo = timedelta(days=7 if visao.tipo == SEMANA else visao.dias) * sentido
    return Visao(visao.tipo, visao.inicio + passo, visao.fim + passo)


def dentro_dos_limites(visao: Visao) -> bool:
    return DATA_MINIMA <= visao.inicio and visao.fim <= DATA_MAXIMA


def parametros(visao: Visao) -> dict[str, str]:
    if visao.tipo == PERIODO:
        return {"visao": PERIODO, "inicio": visao.inicio.isoformat(), "fim": visao.fim.isoformat()}
    return {"visao": visao.tipo, "data": visao.inicio.isoformat()}


def navegacao(visao: Visao, hoje: date) -> dict[str, dict[str, str] | None]:
    if visao.tipo == PERIODO:
        atual = Visao(PERIODO, hoje, hoje + timedelta(days=visao.dias - 1))
    else:
        atual = intervalo_da_visao(visao.tipo, hoje)
    links = {"anterior": deslocar_visao(visao, -1), "proximo": deslocar_visao(visao, 1), "hoje": atual}
    return {nome: parametros(v) if dentro_dos_limites(v) else None for nome, v in links.items()}


def _consulta_sessoes(funcionario_id: int, inicio: date, fim: date, *colunas):
    return (
        select(*colunas)
        .select_from(Sessao)
        .join(Plano, Plano.id == Sessao.plano_id)
        .where(Plano.funcionario_id == funcionario_id)
        .where(Sessao.status.in_(STATUS_VISIVEIS))
        .where(Sessao.data_hora >= datetime.combine(inicio, time.min))
        .where(Sessao.data_hora < datetime.combine(fim + timedelta(days=1), time.min))
    )


def consultas_do_profissional(
    db: Session, funcionario_id: int, inicio: date, fim: date, hoje: date,
) -> list[ConsultaAgenda]:
    colunas = (
        Sessao.data_hora, Sessao.status, Sessao.foi_remarcada,
        Paciente.nome, Paciente.data_nascimento, Especialidade.nome, Plano.observacao,
    )
    consulta = (
        _consulta_sessoes(funcionario_id, inicio, fim, *colunas)
        .join(Paciente, Paciente.id == Plano.paciente_id)
        .join(Especialidade, Especialidade.id == Plano.especialidade_id)
        .order_by(Sessao.data_hora, Sessao.id)
    )
    return [
        ConsultaAgenda(
            inicio=data_hora,
            fim=data_hora + DURACAO_SESSAO,
            atendido=nome,
            idade=calcular_idade(nascimento, hoje),
            especialidade=especialidade,
            observacao=(observacao or "").strip() or None,
            status=status,
            remarcada=remarcada,
        )
        for data_hora, status, remarcada, nome, nascimento, especialidade, observacao in db.execute(consulta)
    ]


def contar_consultas_do_dia(db: Session, funcionario_id: int, dia: date) -> int:
    consulta = _consulta_sessoes(funcionario_id, dia, dia, func.count(Sessao.id))
    return db.scalar(consulta)


def agrupar_por_dia(visao: Visao, consultas: list[ConsultaAgenda]) -> list[DiaAgenda]:
    por_dia: dict[date, list[ConsultaAgenda]] = {}
    for consulta in consultas:
        por_dia.setdefault(consulta.inicio.date(), []).append(consulta)

    dias = []
    for deslocamento in range(visao.dias):
        dia = visao.inicio + timedelta(days=deslocamento)
        if dia.weekday() >= 5 and dia not in por_dia and visao.tipo != DIA:
            continue
        dias.append(DiaAgenda(dia, por_dia.get(dia, []), eh_feriado(dia)))
    return dias


def agrupar_dias_vazios(dias: list[DiaAgenda], hoje: date) -> list[DiaAgenda | DiasSemConsulta]:
    itens: list[DiaAgenda | DiasSemConsulta] = []
    sequencia: list[DiaAgenda] = []

    def fechar_sequencia():
        if len(sequencia) == 1:
            itens.append(sequencia[0])
        elif sequencia:
            itens.append(DiasSemConsulta(sequencia[0].data, sequencia[-1].data))
        sequencia.clear()

    for dia in dias:
        if not dia.consultas and not dia.feriado and dia.data != hoje:
            sequencia.append(dia)
            continue
        fechar_sequencia()
        itens.append(dia)
    fechar_sequencia()
    return itens
