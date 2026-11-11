from dataclasses import dataclass, field, replace
from typing import ClassVar
from datetime import date, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import config
from app.core.ids import ler_data_iso
from app.models.avaliacao import Avaliacao
from app.models.especialidade import Especialidade
from app.models.paciente import Paciente
from app.models.plano import Plano
from app.models.sessao import Sessao, StatusSessao
from app.services.admin_clientes_service import calcular_idade
from app.services.avaliacao_service import pode_registrar, termino
from app.services.plano_service import DURACAO_SESSAO, feriados_do_ano

DIA = "dia"
SEMANA = "semana"
PERIODO = "periodo"
VISOES = (DIA, SEMANA, PERIODO)
A_REGISTRAR = "registrar"  # aba do RF09; não é uma visão de datas

LIMITE_PERIODO_DIAS = 92
# Guarda contra datas absurdas digitadas na URL: a navegação soma dias e estouraria date.max
DATA_MINIMA = date(2000, 1, 1)
DATA_MAXIMA = date(2100, 12, 31)

STATUS_VISIVEIS = (StatusSessao.AGENDADA, StatusSessao.REALIZADA)

INICIO_DA_TARDE = time(12)
DIAS_ATRASO_DESTACADO = 14

MSG_DATA_INVALIDA = "Informe uma data válida."
MSG_FIM_DE_SEMANA = "Hoje não há atendimento: fim de semana."
MSG_FIM_DE_SEMANA_OUTRO_DIA = "Não há atendimento: fim de semana."
# O texto das constantes MSG_* entra no id de testes parametrizados: mudar o texto muda o id
MSG_PERIODO_INCOMPLETO = "Informe a data inicial e a data final do período."
MSG_PERIODO_INVERTIDO = "A data final não pode ser anterior à data inicial."
MSG_PERIODO_LONGO = f"O período pode ter no máximo {LIMITE_PERIODO_DIAS} dias. Escolha um intervalo menor."
TEXTO_PERIODO_LONGO = f"O período pode ter no máximo {LIMITE_PERIODO_DIAS} dias. Escolha uma data até {{limite}}."
TEXTO_PERIODO_INVERTIDO = "A data final não pode ser anterior à data inicial. Escolha uma data a partir de {limite}."

CAMPO_INICIO = "inicio"
CAMPO_FIM = "fim"


class PeriodoInvalido(Exception):
    def __init__(self, mensagem: str, campos: tuple[str, ...] = (), texto: str | None = None):
        super().__init__(mensagem)
        self.mensagem = mensagem
        self.campos = campos
        self.texto = texto or mensagem


@dataclass(frozen=True)
class Visao:
    tipo: str
    inicio: date
    fim: date

    @property
    def dias(self) -> int:
        return (self.fim - self.inicio).days + 1


@dataclass(frozen=True)
class EstadoConsulta:
    chave: str
    rotulo: str


AGENDADA = EstadoConsulta("agendada", "Agendada")
EM_ANDAMENTO = EstadoConsulta("em-andamento", "Em andamento")
AGUARDANDO_REGISTRO = EstadoConsulta("aguardando-registro", "Aguardando registro")
AVALIACAO_ENVIADA = EstadoConsulta("avaliacao-enviada", "Avaliação enviada")
NAO_COMPARECEU = EstadoConsulta("nao-compareceu", "Não compareceu")
REALIZADA = EstadoConsulta("realizada", "Realizada")  # sessão realizada sem avaliação (dado anterior ao RF09)


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
    sessao_id: int = 0
    avaliacao_id: int | None = None
    comparecimento: bool | None = None
    tem_arquivo: bool = False
    pode_registrar: bool = False
    estado: EstadoConsulta | None = None


@dataclass(frozen=True)
class DiaAgenda:
    data: date
    consultas: list[ConsultaAgenda]
    feriado: bool
    nome_feriado: str | None = None
    grupo: ClassVar[bool] = False


@dataclass(frozen=True)
class DiasSemConsulta:
    inicio: date
    fim: date
    quantidade: int = field(default=0, compare=False)
    grupo: ClassVar[bool] = True


@dataclass(frozen=True)
class SemanaAgenda:
    inicio: date
    fim: date
    itens: list[DiaAgenda | DiasSemConsulta]


@dataclass(frozen=True)
class FaixaSemAtendimento:
    inicio: time
    fim: time

    @property
    def texto(self) -> str:
        return f"{self.inicio:%H:%M} às {self.fim:%H:%M}, sem atendimento"


@dataclass(frozen=True)
class TurnosDoDia:
    manha: list[ConsultaAgenda]
    tarde: list[ConsultaAgenda]
    faixas: list[FaixaSemAtendimento]


@dataclass(frozen=True)
class Atalho:
    rotulo: str
    inicio: date
    fim: date


@dataclass(frozen=True)
class PendenteDeRegistro:
    consulta: object  # avaliacao_service.SessaoParaRegistro
    quando: str
    destacado: bool


# --- Estado da consulta (RNF14: agora sempre por parâmetro) -------------------------------------

def em_andamento(sessao: ConsultaAgenda, agora: datetime) -> bool:
    return sessao.inicio <= agora < termino(sessao.inicio)


def estado_da_consulta(consulta: ConsultaAgenda, agora: datetime) -> EstadoConsulta:
    if consulta.status == StatusSessao.REALIZADA:
        if consulta.avaliacao_id is None:
            return REALIZADA
        if consulta.comparecimento is False:
            return NAO_COMPARECEU
        return AVALIACAO_ENVIADA if consulta.tem_arquivo else REALIZADA
    if em_andamento(consulta, agora):
        return EM_ANDAMENTO
    if pode_registrar(consulta.status, consulta.inicio, agora):
        return AGUARDANDO_REGISTRO
    return AGENDADA


def resumo_das_consultas(consultas: list[ConsultaAgenda]) -> str:
    total = len(consultas)
    if total == 0:
        return "Nenhuma consulta"
    partes = ["1 consulta" if total == 1 else f"{total} consultas"]
    andamento = sum(1 for c in consultas if c.estado == EM_ANDAMENTO)
    a_registrar = sum(1 for c in consultas if c.estado == AGUARDANDO_REGISTRO)
    if andamento:
        partes.append(f"{andamento} em andamento")
    if a_registrar:
        partes.append(f"{a_registrar} aguardando registro")
    return ", ".join(partes)


def dias_desde(momento: datetime, agora: datetime) -> int:
    return (agora.date() - momento.date()).days


def ha_quanto_tempo(momento: datetime, agora: datetime) -> str:
    dias = dias_desde(momento, agora)
    if dias <= 0:
        return "hoje"
    return "ontem" if dias == 1 else f"há {dias} dias"


def atraso_destacado(momento: datetime, agora: datetime) -> bool:
    return dias_desde(momento, agora) >= DIAS_ATRASO_DESTACADO


def pendentes_de_registro(consultas: list, agora: datetime) -> list[PendenteDeRegistro]:
    return [
        PendenteDeRegistro(c, ha_quanto_tempo(c.data_hora, agora), atraso_destacado(c.data_hora, agora))
        for c in consultas
    ]


# --- Grade do dia (RN08 lida de config.HORARIOS_ATENDIMENTO) -------------------------------------

def faixas_sem_atendimento(horarios: list[int] | None = None) -> list[FaixaSemAtendimento]:
    grade = sorted(config.HORARIOS_ATENDIMENTO if horarios is None else horarios)
    faixas = []
    for anterior, seguinte in zip(grade, grade[1:]):
        fim_anterior = (datetime.combine(date.min, time(anterior)) + DURACAO_SESSAO).time()
        if fim_anterior < time(seguinte):
            faixas.append(FaixaSemAtendimento(fim_anterior, time(seguinte)))
    return faixas


def separar_turnos(consultas: list[ConsultaAgenda], horarios: list[int] | None = None) -> TurnosDoDia:
    manha = [c for c in consultas if c.inicio.time() < INICIO_DA_TARDE]
    tarde = [c for c in consultas if c.inicio.time() >= INICIO_DA_TARDE]
    faixas = [f for f in faixas_sem_atendimento(horarios) if f.inicio <= INICIO_DA_TARDE <= f.fim]
    return TurnosDoDia(manha, tarde, faixas)


def motivo_sem_atendimento_no_dia(dia: date, hoje: date) -> str | None:
    if dia.weekday() in config.DIAS_UTEIS:
        return None
    return MSG_FIM_DE_SEMANA if dia == hoje else MSG_FIM_DE_SEMANA_OUTRO_DIA


# --- Intervalo de cada visão ---------------------------------------------------------------------

def interpretar_data(texto: str | None, campo: str | None = None) -> date:
    valor = ler_data_iso(texto)
    if valor is None or not DATA_MINIMA <= valor <= DATA_MAXIMA:
        raise PeriodoInvalido(MSG_DATA_INVALIDA, (campo,) if campo else ())
    return valor


def data_limite_do_periodo(inicio: date) -> date:
    return inicio + timedelta(days=LIMITE_PERIODO_DIAS - 1)


def _data_br(dia: date) -> str:
    return dia.strftime("%d/%m/%Y")


def intervalo_da_visao(tipo: str, referencia: date, inicio: date | None = None, fim: date | None = None) -> Visao:
    if tipo == DIA:
        return Visao(DIA, referencia, referencia)
    if tipo == PERIODO:
        if inicio is None or fim is None:
            faltando = tuple(c for c, v in ((CAMPO_INICIO, inicio), (CAMPO_FIM, fim)) if v is None)
            raise PeriodoInvalido(MSG_PERIODO_INCOMPLETO, faltando)
        if fim < inicio:
            raise PeriodoInvalido(
                MSG_PERIODO_INVERTIDO, (CAMPO_FIM,), TEXTO_PERIODO_INVERTIDO.format(limite=_data_br(inicio)),
            )
        limite = data_limite_do_periodo(inicio)
        if fim > limite:
            raise PeriodoInvalido(
                MSG_PERIODO_LONGO, (CAMPO_FIM,), TEXTO_PERIODO_LONGO.format(limite=_data_br(min(limite, DATA_MAXIMA))),
            )
        return Visao(PERIODO, inicio, fim)
    segunda = referencia - timedelta(days=referencia.weekday())
    return Visao(SEMANA, segunda, segunda + timedelta(days=4))


def _ler_periodo(inicio: str | None, fim: str | None, hoje: date) -> Visao:
    datas, invalidos = {}, []
    for campo, texto in ((CAMPO_INICIO, inicio), (CAMPO_FIM, fim)):
        try:
            datas[campo] = interpretar_data(texto, campo) if texto else None
        except PeriodoInvalido:
            invalidos.append(campo)
    if invalidos:
        raise PeriodoInvalido(MSG_DATA_INVALIDA, tuple(invalidos))
    return intervalo_da_visao(PERIODO, hoje, datas[CAMPO_INICIO], datas[CAMPO_FIM])


def escolher_visao(
    tipo: str | None, data: str | None, inicio: str | None, fim: str | None, hoje: date,
) -> tuple[Visao, PeriodoInvalido | None]:
    tipo = tipo if tipo in VISOES else SEMANA
    try:
        if tipo == PERIODO:
            return _ler_periodo(inicio, fim, hoje), None
        return intervalo_da_visao(tipo, interpretar_data(data) if data else hoje), None
    except PeriodoInvalido as erro:
        return intervalo_da_visao(SEMANA, hoje), erro


def montar_visao(
    tipo: str | None, data: str | None, inicio: str | None, fim: str | None, hoje: date,
) -> tuple[Visao, str | None]:
    visao, erro = escolher_visao(tipo, data, inicio, fim, hoje)
    return visao, erro.mensagem if erro else None


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


def _ultimo_dia_do_mes(dia: date) -> date:
    proximo = (dia.replace(day=28) + timedelta(days=4)).replace(day=1)
    return proximo - timedelta(days=1)


def atalhos_do_periodo(hoje: date) -> list[Atalho]:
    inicio_mes = hoje.replace(day=1)
    proximo_mes = _ultimo_dia_do_mes(hoje) + timedelta(days=1)
    return [
        Atalho("Próximos 7 dias", hoje, hoje + timedelta(days=6)),
        Atalho("Próximos 30 dias", hoje, hoje + timedelta(days=29)),
        Atalho("Este mês", inicio_mes, _ultimo_dia_do_mes(hoje)),
        Atalho("Próximo mês", proximo_mes, _ultimo_dia_do_mes(proximo_mes)),
    ]


# --- Consultas ao banco --------------------------------------------------------------------------

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
    db: Session, funcionario_id: int, inicio: date, fim: date, hoje: date, agora: datetime | None = None,
) -> list[ConsultaAgenda]:
    colunas = (
        Sessao.data_hora, Sessao.status, Sessao.foi_remarcada,
        Paciente.nome, Paciente.data_nascimento, Especialidade.nome, Plano.observacao,
        Sessao.id, Avaliacao.id, Avaliacao.comparecimento, Avaliacao.arquivo_url,
    )
    consulta = (
        _consulta_sessoes(funcionario_id, inicio, fim, *colunas)
        .join(Paciente, Paciente.id == Plano.paciente_id)
        .join(Especialidade, Especialidade.id == Plano.especialidade_id)
        .outerjoin(Avaliacao, Avaliacao.sessao_id == Sessao.id)
        .order_by(Sessao.data_hora, Sessao.id)
    )
    consultas = [
        ConsultaAgenda(
            inicio=data_hora,
            fim=data_hora + DURACAO_SESSAO,
            atendido=nome,
            idade=calcular_idade(nascimento, hoje),
            especialidade=especialidade,
            observacao=(observacao or "").strip() or None,
            status=status,
            remarcada=remarcada,
            sessao_id=sessao_id,
            avaliacao_id=avaliacao_id,
            comparecimento=comparecimento,
            tem_arquivo=bool(arquivo),
            pode_registrar=agora is not None and pode_registrar(status, data_hora, agora),
        )
        for (
            data_hora, status, remarcada, nome, nascimento, especialidade, observacao,
            sessao_id, avaliacao_id, comparecimento, arquivo,
        ) in db.execute(consulta)
    ]
    if agora is None:
        return consultas
    return [replace(c, estado=estado_da_consulta(c, agora)) for c in consultas]


def contar_consultas_do_dia(db: Session, funcionario_id: int, dia: date) -> int:
    consulta = _consulta_sessoes(funcionario_id, dia, dia, func.count(Sessao.id))
    return db.scalar(consulta)


# --- Agrupamento (funções puras) -----------------------------------------------------------------

def agrupar_por_dia(visao: Visao, consultas: list[ConsultaAgenda]) -> list[DiaAgenda]:
    por_dia: dict[date, list[ConsultaAgenda]] = {}
    for consulta in consultas:
        por_dia.setdefault(consulta.inicio.date(), []).append(consulta)

    feriados = {}
    dias = []
    for deslocamento in range(visao.dias):
        dia = visao.inicio + timedelta(days=deslocamento)
        if dia.weekday() >= 5 and dia not in por_dia and visao.tipo != DIA:
            continue
        if dia.year not in feriados:
            feriados[dia.year] = feriados_do_ano(dia.year)
        nome = feriados[dia.year].get(dia)
        dias.append(DiaAgenda(dia, por_dia.get(dia, []), nome is not None, nome))
    return dias


def agrupar_dias_vazios(dias: list[DiaAgenda], hoje: date) -> list[DiaAgenda | DiasSemConsulta]:
    itens: list[DiaAgenda | DiasSemConsulta] = []
    sequencia: list[DiaAgenda] = []

    def fechar_sequencia():
        if len(sequencia) == 1:
            itens.append(sequencia[0])
        elif sequencia:
            itens.append(DiasSemConsulta(sequencia[0].data, sequencia[-1].data, len(sequencia)))
        sequencia.clear()

    for dia in dias:
        if not dia.consultas and not dia.feriado and dia.data != hoje:
            sequencia.append(dia)
            continue
        fechar_sequencia()
        itens.append(dia)
    fechar_sequencia()
    return itens


def agrupar_por_semana(dias: list[DiaAgenda], hoje: date) -> list[SemanaAgenda]:
    por_semana: dict[date, list[DiaAgenda]] = {}
    for dia in dias:
        por_semana.setdefault(dia.data - timedelta(days=dia.data.weekday()), []).append(dia)
    return [
        SemanaAgenda(lista[0].data, lista[-1].data, agrupar_dias_vazios(lista, hoje))
        for lista in por_semana.values()
    ]
