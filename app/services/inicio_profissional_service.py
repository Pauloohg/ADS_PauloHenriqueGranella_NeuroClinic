from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import config
from app.core.templating import NOMES_DIAS, NOMES_MESES
from app.models.especialidade import Especialidade
from app.models.funcionario import Funcionario
from app.models.paciente import Paciente
from app.models.plano import Plano
from app.models.sessao import Sessao, StatusSessao
from app.services.admin_clientes_service import calcular_idade
from app.services.agenda_service import ConsultaAgenda, consultas_do_profissional
from app.services.avaliacao_service import consulta_contar_a_registrar, termino
from app.services.plano_service import DURACAO_SESSAO, feriados_do_ano

LIMITE_PROXIMAS = 5
MSG_FIM_DE_SEMANA = "Hoje não há atendimento: fim de semana."
MSG_FERIADO = "Hoje é feriado nacional: {nome}."


@dataclass(frozen=True)
class ConsultaDoDia:
    consulta: ConsultaAgenda
    em_andamento: bool


@dataclass(frozen=True)
class ItemLinhaDoTempo:
    horario: time | None
    consultas: tuple[ConsultaDoDia, ...] = ()
    intervalo: str | None = None


@dataclass(frozen=True)
class ConsultaProxima:
    inicio: datetime
    atendido: str
    idade: int
    dia: str


@dataclass(frozen=True)
class InicioProfissional:
    titulo_dia: str
    subtitulo: str
    especialidade: str | None
    a_registrar: int
    feriado: bool
    sem_atendimento: str | None
    linha_do_tempo: list[ItemLinhaDoTempo]
    proximas: list[ConsultaProxima]


# --- Funções puras (RNF14: agora sempre por parâmetro) -------------------------------------------

def em_andamento(sessao: ConsultaAgenda, agora: datetime) -> bool:
    return sessao.inicio <= agora < termino(sessao.inicio)


def titulo_do_dia(dia: date) -> str:
    nome = NOMES_DIAS[dia.weekday()] + ("-feira" if dia.weekday() < 5 else "")
    return f"{nome}, {dia.day} de {NOMES_MESES[dia.month - 1]}"


def _data_curta(dia: date, hoje: date) -> str:
    return dia.strftime("%d/%m") if dia.year == hoje.year else dia.strftime("%d/%m/%Y")


def dia_abreviado(dia: date, hoje: date) -> str:
    return f"{NOMES_DIAS[dia.weekday()][:3].lower()}, {_data_curta(dia, hoje)}"


def quando_proxima(inicio: datetime, hoje: date) -> str:
    if inicio.date() == hoje + timedelta(days=1):
        return f"amanhã, às {inicio:%H:%M}"
    dia = inicio.date()
    artigo = "na" if dia.weekday() < 5 else "no"
    nome = NOMES_DIAS[dia.weekday()].lower() + ("-feira" if dia.weekday() < 5 else "")
    return f"{artigo} {nome}, {_data_curta(dia, hoje)}, às {inicio:%H:%M}"


def motivo_sem_atendimento(dia: date) -> str | None:
    nome = feriados_do_ano(dia.year).get(dia)
    if nome:
        return MSG_FERIADO.format(nome=nome)
    if dia.weekday() not in config.DIAS_UTEIS:
        return MSG_FIM_DE_SEMANA
    return None


def subtitulo(consultas: list[ConsultaAgenda], proxima: ConsultaProxima | None, agora: datetime) -> str:
    total = len(consultas)
    if total == 0:
        texto = "Hoje você não tem consultas."
        return f"{texto} A próxima é {quando_proxima(proxima.inicio, agora.date())}." if proxima else texto

    andamento = next((c for c in consultas if em_andamento(c, agora)), None)
    seguinte = next((c for c in consultas if c.inicio > agora), None)
    if andamento is None and seguinte is None:
        if total == 1:
            return "Hoje você teve 1 consulta, que já aconteceu."
        return f"Hoje você teve {total} consultas, todas já aconteceram."

    if total == 1:
        if andamento:
            return f"Hoje você tem 1 consulta, a de {andamento.atendido}, que está em andamento."
        return f"Hoje você tem 1 consulta, que começa às {seguinte.inicio:%H:%M}."

    texto = f"Hoje você tem {total} consultas."
    if andamento and seguinte:
        return f"{texto} A que está em andamento é a de {andamento.atendido}, e a próxima começa às {seguinte.inicio:%H:%M}."
    if andamento:
        return f"{texto} A que está em andamento é a de {andamento.atendido}."
    return f"{texto} A próxima começa às {seguinte.inicio:%H:%M}."


def linha_do_tempo(
    consultas: list[ConsultaAgenda], agora: datetime, horarios: list[int] | None = None,
) -> list[ItemLinhaDoTempo]:
    grade = sorted(config.HORARIOS_ATENDIMENTO if horarios is None else horarios)
    por_inicio: dict[time, list[ConsultaDoDia]] = {}
    for consulta in consultas:
        por_inicio.setdefault(consulta.inicio.time(), []).append(ConsultaDoDia(consulta, em_andamento(consulta, agora)))

    itens = [(time(hora), 1, ItemLinhaDoTempo(time(hora), tuple(por_inicio.pop(time(hora), ())))) for hora in grade]
    # Sessão fora da grade (dado antigo) entra na posição do seu horário
    itens += [(inicio, 1, ItemLinhaDoTempo(inicio, tuple(lista))) for inicio, lista in por_inicio.items()]
    for anterior, seguinte in zip(grade, grade[1:]):
        fim_anterior = (datetime.combine(date.min, time(anterior)) + DURACAO_SESSAO).time()
        if fim_anterior < time(seguinte):
            texto = f"{fim_anterior:%H:%M} às {seguinte:02d}:00, sem atendimento"
            itens.append((fim_anterior, 0, ItemLinhaDoTempo(None, intervalo=texto)))
    return [item for _, _, item in sorted(itens, key=lambda par: (par[0], par[1]))]


# --- Consultas ao banco (no máximo 3 comandos SQL por página) -------------------------------------

def proximas_consultas(db: Session, funcionario_id: int, hoje: date, limite: int = LIMITE_PROXIMAS) -> list[ConsultaProxima]:
    consulta = (
        select(Sessao.data_hora, Paciente.nome, Paciente.data_nascimento)
        .select_from(Sessao)
        .join(Plano, Plano.id == Sessao.plano_id)
        .join(Paciente, Paciente.id == Plano.paciente_id)
        .where(Plano.funcionario_id == funcionario_id)
        .where(Sessao.status == StatusSessao.AGENDADA)
        .where(Sessao.data_hora >= datetime.combine(hoje + timedelta(days=1), time.min))
        .order_by(Sessao.data_hora, Sessao.id)
        .limit(limite)
    )
    return [
        ConsultaProxima(data_hora, nome, calcular_idade(nascimento, hoje), dia_abreviado(data_hora.date(), hoje))
        for data_hora, nome, nascimento in db.execute(consulta)
    ]


def especialidade_e_a_registrar(db: Session, funcionario_id: int, agora: datetime) -> tuple[str | None, int]:
    contagem = consulta_contar_a_registrar(funcionario_id, agora).scalar_subquery()
    linha = db.execute(
        select(Especialidade.nome, contagem)
        .select_from(Funcionario)
        .outerjoin(Especialidade, Especialidade.id == Funcionario.especialidade_id)
        .where(Funcionario.usuario_id == funcionario_id)
    ).first()
    return (linha[0], linha[1] or 0) if linha else (None, 0)


def montar_inicio(db: Session, funcionario_id: int, agora: datetime) -> InicioProfissional:
    hoje = agora.date()
    consultas = consultas_do_profissional(db, funcionario_id, hoje, hoje, hoje, agora)
    proximas = proximas_consultas(db, funcionario_id, hoje)
    especialidade, a_registrar = especialidade_e_a_registrar(db, funcionario_id, agora)

    sem_atendimento = motivo_sem_atendimento(hoje)
    # Fim de semana ou feriado: sem grade; se houver consulta (dado antigo), ela aparece sozinha, como na agenda
    itens = linha_do_tempo(consultas, agora, [] if sem_atendimento else None)
    return InicioProfissional(
        titulo_dia=titulo_do_dia(hoje),
        subtitulo=subtitulo(consultas, proximas[0] if proximas else None, agora),
        especialidade=especialidade,
        a_registrar=a_registrar,
        feriado=hoje in feriados_do_ano(hoje.year),
        sem_atendimento=sem_atendimento if not consultas else None,
        linha_do_tempo=itens,
        proximas=proximas,
    )
