from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache

import holidays
from dateutil.relativedelta import relativedelta
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import (
    ANTECEDENCIA_PRIMEIRA_SESSAO_HORAS,
    DIAS_UTEIS,
    DURACAO_SESSAO_MINUTOS,
    HORARIOS_ATENDIMENTO,
    PRAZO_PAGAMENTO_HORAS,
)
from app.core.fuso import utc_para_local
from app.models.especialidade import Especialidade
from app.models.funcionario import Funcionario
from app.models.plano import Plano
from app.models.sessao import Sessao, StatusSessao
from app.services.funcionario_service import STATUS_QUE_PRENDEM

DURACAO_SESSAO = timedelta(minutes=DURACAO_SESSAO_MINUTOS)
PRAZO_PAGAMENTO = timedelta(hours=PRAZO_PAGAMENTO_HORAS)
ANTECEDENCIA_PRIMEIRA_SESSAO = timedelta(hours=ANTECEDENCIA_PRIMEIRA_SESSAO_HORAS)
FREQUENCIAS = (1, 2, 3)

JANELA_POR_DURACAO = {
    "quinzenal": relativedelta(days=14),
    "mensal": relativedelta(months=1),
    "bimestral": relativedelta(months=2),
    "anual": relativedelta(months=12),
}


class PlanoInvalido(ValueError):
    pass


class DiasSemanaInvalidos(PlanoInvalido):
    pass


class HorarioInvalido(PlanoInvalido):
    pass


class FrequenciaInvalida(PlanoInvalido):
    pass


class DuracaoInvalida(PlanoInvalido):
    pass


class ProfissionalIndisponivel(PlanoInvalido):
    pass


@dataclass(frozen=True)
class SimulacaoSessoes:
    sessoes: list[datetime]
    quantidade: int
    data_inicio: date
    data_fim: date
    feriados_pulados: list[date]


@dataclass(frozen=True)
class SimulacaoPlano:
    funcionario_id: int
    especialidade_id: int
    sessoes: list[datetime]
    quantidade: int
    valor_sessao: Decimal
    valor_total: Decimal
    data_inicio: date
    data_fim: date
    feriados_pulados: list[date]
    conflitos: list[datetime]


def _lista_horarios() -> str:
    rotulos = [f"{hora:02d}:00" for hora in HORARIOS_ATENDIMENTO]
    return ", ".join(rotulos[:-1]) + " ou " + rotulos[-1]


def validar_dias_semana(dias_semana: list[int], frequencia_semanal: int) -> list[int]:
    if not dias_semana:
        raise DiasSemanaInvalidos("Escolha pelo menos um dia da semana.")
    if any(type(dia) is not int or not 0 <= dia <= 6 for dia in dias_semana):
        raise DiasSemanaInvalidos("Dia da semana inválido.")
    if len(set(dias_semana)) != len(dias_semana):
        raise DiasSemanaInvalidos("Escolha dias da semana diferentes.")
    if len(dias_semana) != frequencia_semanal:
        raise DiasSemanaInvalidos(f"Escolha exatamente {frequencia_semanal} dia(s) da semana.")
    return sorted(dias_semana)


def validar_horario(horario: time) -> time:
    if (horario.minute, horario.second, horario.microsecond) != (0, 0, 0) or horario.hour not in HORARIOS_ATENDIMENTO:
        raise HorarioInvalido(f"Escolha um horário em hora cheia: {_lista_horarios()}.")
    return horario


def validar_duracao(duracao: str) -> str:
    if duracao not in JANELA_POR_DURACAO:
        raise DuracaoInvalida("Escolha a duração do plano: quinzenal, mensal, bimestral ou anual.")
    return duracao


def _validar_agenda(dias_semana: list[int], frequencia_semanal: int, horario: time, duracao: str) -> list[int]:
    if type(frequencia_semanal) is not int or frequencia_semanal not in FREQUENCIAS:
        raise FrequenciaInvalida("A frequência deve ser de 1, 2 ou 3 vezes por semana.")
    validar_duracao(duracao)
    dias = validar_dias_semana(dias_semana, frequencia_semanal)
    if any(dia not in DIAS_UTEIS for dia in dias):
        raise DiasSemanaInvalidos("A clínica atende somente de segunda a sexta-feira.")
    validar_horario(horario)
    return dias


# Único ponto que instancia holidays.Brazil: sem language explícito, os nomes seguem o locale do sistema (sai inglês)
@lru_cache
def feriados_do_ano(ano: int) -> holidays.HolidayBase:
    return holidays.Brazil(years=ano, language="pt_BR", expand=False)


def eh_feriado(dia: date) -> bool:
    return dia in feriados_do_ano(dia.year)


def _datas_dos_dias(inicio: date, dias: list[int]):
    dia = inicio
    while True:
        if dia.weekday() in dias:
            yield dia
        dia += timedelta(days=1)


def _datas_ate(inicio: date, fim_exclusivo: date, dias: list[int]):
    for dia in _datas_dos_dias(inicio, dias):
        if dia >= fim_exclusivo:
            return
        yield dia


def simular_sessoes(
    dias_semana: list[int], horario: time, frequencia_semanal: int, duracao: str, agora: datetime
) -> SimulacaoSessoes:
    dias = _validar_agenda(dias_semana, frequencia_semanal, horario, duracao)

    limite = agora + ANTECEDENCIA_PRIMEIRA_SESSAO  # RN10
    primeiro = next(d for d in _datas_dos_dias(limite.date(), dias) if datetime.combine(d, horario) >= limite)
    fim_janela = primeiro + JANELA_POR_DURACAO[duracao]  # exclusivo
    quantidade = sum(1 for _ in _datas_ate(primeiro, fim_janela, dias))

    sessoes: list[datetime] = []
    pulados: list[date] = []
    for dia in _datas_dos_dias(primeiro, dias):
        if len(sessoes) == quantidade:
            break
        if eh_feriado(dia):
            pulados.append(dia)
        else:
            sessoes.append(datetime.combine(dia, horario))

    return SimulacaoSessoes(sessoes, quantidade, sessoes[0].date(), sessoes[-1].date(), pulados)


def nome_do_feriado(dia: date) -> str:
    return feriados_do_ano(dia.year).get(dia, "")


def calcular_valor_total(valor_sessao: Decimal, quantidade: int) -> Decimal:
    return (valor_sessao * quantidade).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def ocupa_horario(status: StatusSessao, plano_criado_em_utc: datetime, agora: datetime) -> bool:
    if status != StatusSessao.PENDENTE_PAGAMENTO:
        return True
    return utc_para_local(plano_criado_em_utc) + PRAZO_PAGAMENTO >= agora  # RN09


def buscar_horarios_ocupados(
    db: Session, funcionario_id: int, inicio: datetime, fim: datetime, agora: datetime
) -> list[datetime]:
    linhas = db.execute(
        select(Sessao.data_hora, Sessao.status, Plano.criado_em)
        .join(Plano, Plano.id == Sessao.plano_id)
        .where(Plano.funcionario_id == funcionario_id)
        .where(Sessao.status.in_(STATUS_QUE_PRENDEM))
        .where(Sessao.data_hora > inicio - DURACAO_SESSAO)
        .where(Sessao.data_hora < fim + DURACAO_SESSAO)
    )
    return sorted(data_hora for data_hora, status, criado_em in linhas if ocupa_horario(status, criado_em, agora))


def comparar_conflitos(propostas: list[datetime], ocupados_ordenados: list[datetime]) -> list[datetime]:
    conflitos = []
    for proposta in propostas:
        # Primeiro ocupado que termina depois do início da proposta; conflita se começar antes do fim dela
        indice = bisect_right(ocupados_ordenados, proposta - DURACAO_SESSAO)
        if indice < len(ocupados_ordenados) and ocupados_ordenados[indice] < proposta + DURACAO_SESSAO:
            conflitos.append(proposta)
    return sorted(conflitos)


def verificar_conflitos(db: Session, funcionario_id: int, datas_hora: list[datetime], agora: datetime) -> list[datetime]:
    if not datas_hora:
        return []
    ocupados = buscar_horarios_ocupados(db, funcionario_id, min(datas_hora), max(datas_hora), agora)
    return comparar_conflitos(datas_hora, ocupados)


def disponibilidade_semanal(db: Session, funcionario_id: int, duracao: str, agora: datetime) -> dict[int, list[int]]:
    validar_duracao(duracao)
    simulacoes = {
        (dia, hora): simular_sessoes([dia], time(hora), 1, duracao, agora).sessoes
        for dia in DIAS_UTEIS
        for hora in HORARIOS_ATENDIMENTO
    }
    todas = [sessao for sessoes in simulacoes.values() for sessao in sessoes]
    ocupados = buscar_horarios_ocupados(db, funcionario_id, min(todas), max(todas), agora)

    livres: dict[int, list[int]] = {dia: [] for dia in DIAS_UTEIS}
    for (dia, hora), sessoes in simulacoes.items():
        if not comparar_conflitos(sessoes, ocupados):
            livres[dia].append(hora)
    return livres


def buscar_funcionario_ativo(db: Session, funcionario_id: int) -> Funcionario:
    funcionario = db.get(Funcionario, funcionario_id)
    if funcionario is None or not funcionario.ativo:
        raise ProfissionalIndisponivel("Este profissional não está disponível para novos planos.")
    return funcionario


def simular_plano(
    db: Session,
    funcionario_id: int,
    dias_semana: list[int],
    horario: time,
    frequencia_semanal: int,
    duracao: str,
    agora: datetime,
) -> SimulacaoPlano:
    funcionario = buscar_funcionario_ativo(db, funcionario_id)
    especialidade = db.get(Especialidade, funcionario.especialidade_id)

    simulacao = simular_sessoes(dias_semana, horario, frequencia_semanal, duracao, agora)
    return SimulacaoPlano(
        funcionario_id=funcionario_id,
        especialidade_id=especialidade.id,
        sessoes=simulacao.sessoes,
        quantidade=simulacao.quantidade,
        valor_sessao=especialidade.valor_sessao,
        valor_total=calcular_valor_total(especialidade.valor_sessao, simulacao.quantidade),
        data_inicio=simulacao.data_inicio,
        data_fim=simulacao.data_fim,
        feriados_pulados=simulacao.feriados_pulados,
        conflitos=verificar_conflitos(db, funcionario_id, simulacao.sessoes, agora),
    )
