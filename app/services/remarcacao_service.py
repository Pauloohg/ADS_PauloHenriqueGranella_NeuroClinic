from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import ANTECEDENCIA_REMARCACAO_HORAS, DIAS_UTEIS, HORARIOS_ATENDIMENTO, JANELA_REMARCACAO_DIAS
from app.models.especialidade import Especialidade
from app.models.historico_remarcacao import HistoricoRemarcacao
from app.models.paciente import Paciente
from app.models.plano import Plano
from app.models.sessao import Sessao, StatusSessao
from app.models.usuario import Usuario
from app.services import plano_service

ANTECEDENCIA_REMARCACAO = timedelta(hours=ANTECEDENCIA_REMARCACAO_HORAS)

MOTIVO_PENDENTE = "Disponível após o pagamento"
MOTIVO_REALIZADA = "Consulta já realizada"
MOTIVO_JA_PASSOU = "Horário já passou"
MOTIVO_MENOS_DE_4H = f"Faltam menos de {ANTECEDENCIA_REMARCACAO_HORAS} horas"

MSG_PENDENTE = (
    "Esta consulta ainda aguarda o pagamento do plano. A remarcação fica disponível depois que o pagamento "
    "for confirmado."
)
MSG_REALIZADA = "Esta consulta já foi realizada e não pode ser remarcada."
MSG_JA_PASSOU = "O horário desta consulta já passou, então ela não pode mais ser remarcada."
MSG_RN02 = (
    f"A remarcação precisa ser feita com pelo menos {ANTECEDENCIA_REMARCACAO_HORAS} horas de antecedência do "
    "horário original da consulta. Para esta consulta, o prazo terminou em {limite}."
)
MSG_MESMO_HORARIO = "A consulta já está marcada neste horário. Escolha um horário diferente."
MSG_HORARIO_PASSADO = "Este horário já passou. Escolha um horário a partir de agora."
MSG_FORA_DA_JANELA = f"Escolha uma data entre hoje e os próximos {JANELA_REMARCACAO_DIAS} dias."
MSG_FIM_DE_SEMANA = "A clínica atende somente de segunda a sexta-feira. Escolha um dia útil."
MSG_FERIADO = "{data} é feriado nacional ({nome}) e a clínica não atende. Escolha outra data."
MSG_CONFLITO = (
    "Este horário não está mais livre na agenda do Profissional. Nada foi alterado: escolha outro horário."
)
MSG_ESCOLHA_HORARIO = "Escolha um dos horários livres."

DIA_FORA_DA_JANELA = f"Fora da janela de {JANELA_REMARCACAO_DIAS} dias"
DIA_FIM_DE_SEMANA = "Fim de semana"
DIA_FERIADO = "Feriado nacional"
DIA_SEM_HORARIOS = "Sem horários livres"


class SessaoNaoEncontrada(Exception):
    pass


class RemarcacaoInvalida(Exception):
    def __init__(self, mensagem: str):
        super().__init__(mensagem)
        self.mensagem = mensagem


class ConflitoRemarcacao(RemarcacaoInvalida):
    pass


@dataclass(frozen=True)
class SessaoDoCliente:
    id: int
    plano_id: int
    funcionario_id: int
    data_hora: datetime
    status: StatusSessao
    foi_remarcada: bool
    atendido: str
    profissional: str
    especialidade: str


@dataclass(frozen=True)
class HorariosDoDia:
    data: date
    horarios: list[datetime]
    motivo: str | None


@dataclass(frozen=True)
class ResultadoRemarcacao:
    original: datetime
    nova: datetime
    repetido: bool


def _data_hora_br(valor: datetime) -> str:
    return valor.strftime("%d/%m/%Y às %H:%M")


def _bloqueio(status: StatusSessao, data_hora: datetime, agora: datetime) -> tuple[str, str] | None:
    if status == StatusSessao.PENDENTE_PAGAMENTO:
        return MOTIVO_PENDENTE, MSG_PENDENTE
    if status == StatusSessao.REALIZADA:
        return MOTIVO_REALIZADA, MSG_REALIZADA
    if data_hora <= agora:
        return MOTIVO_JA_PASSOU, MSG_JA_PASSOU
    if data_hora - agora < ANTECEDENCIA_REMARCACAO:  # RN02: exatamente 4h ainda pode
        return MOTIVO_MENOS_DE_4H, MSG_RN02.format(limite=_data_hora_br(data_hora - ANTECEDENCIA_REMARCACAO))
    return None


def motivo_bloqueio(status: StatusSessao, data_hora: datetime, agora: datetime) -> str | None:
    bloqueio = _bloqueio(status, data_hora, agora)
    return bloqueio[0] if bloqueio else None


def validar_sessao_remarcavel(status: StatusSessao, data_hora: datetime, agora: datetime) -> None:
    bloqueio = _bloqueio(status, data_hora, agora)
    if bloqueio:
        raise RemarcacaoInvalida(bloqueio[1])


def janela(agora: datetime) -> tuple[date, date]:
    return agora.date(), agora.date() + timedelta(days=JANELA_REMARCACAO_DIAS)


def validar_novo_horario(atual: datetime, novo: datetime, agora: datetime) -> None:
    if novo == atual:
        raise RemarcacaoInvalida(MSG_MESMO_HORARIO)
    if novo <= agora:
        raise RemarcacaoInvalida(MSG_HORARIO_PASSADO)
    if novo.date() > janela(agora)[1]:
        raise RemarcacaoInvalida(MSG_FORA_DA_JANELA)
    if novo.weekday() not in DIAS_UTEIS:
        raise RemarcacaoInvalida(MSG_FIM_DE_SEMANA)
    try:
        plano_service.validar_horario(novo.time())
    except plano_service.HorarioInvalido as exc:
        raise RemarcacaoInvalida(str(exc)) from None
    if plano_service.eh_feriado(novo.date()):
        nome = plano_service.nome_do_feriado(novo.date())
        raise RemarcacaoInvalida(MSG_FERIADO.format(data=novo.strftime("%d/%m/%Y"), nome=nome))


def interpretar_horario(texto: str) -> datetime:
    try:
        return datetime.fromisoformat(texto.strip())
    except ValueError:
        raise RemarcacaoInvalida(MSG_ESCOLHA_HORARIO) from None


def buscar_sessao_do_cliente(db: Session, cliente_id: int, sessao_id: int) -> SessaoDoCliente:
    linha = db.execute(
        select(
            Sessao.id, Sessao.plano_id, Plano.funcionario_id, Sessao.data_hora, Sessao.status, Sessao.foi_remarcada,
            Paciente.nome, Usuario.nome, Especialidade.nome,
        )
        .join(Plano, Plano.id == Sessao.plano_id)
        .join(Paciente, Paciente.id == Plano.paciente_id)
        .join(Especialidade, Especialidade.id == Plano.especialidade_id)
        .join(Usuario, Usuario.id == Plano.funcionario_id)
        .where(Sessao.id == sessao_id, Paciente.cliente_id == cliente_id)
    ).first()
    if linha is None:
        raise SessaoNaoEncontrada
    return SessaoDoCliente(*linha)


def horarios_livres(db: Session, sessao: SessaoDoCliente, dia: date, agora: datetime) -> HorariosDoDia:
    inicio, fim = janela(agora)
    if not inicio <= dia <= fim:
        return HorariosDoDia(dia, [], DIA_FORA_DA_JANELA)
    if dia.weekday() not in DIAS_UTEIS:
        return HorariosDoDia(dia, [], DIA_FIM_DE_SEMANA)
    if plano_service.eh_feriado(dia):
        return HorariosDoDia(dia, [], DIA_FERIADO)

    candidatos = [
        datetime.combine(dia, time(hora)) for hora in HORARIOS_ATENDIMENTO
        if datetime.combine(dia, time(hora)) > agora and datetime.combine(dia, time(hora)) != sessao.data_hora
    ]
    livres = []
    if candidatos:
        ocupados = plano_service.buscar_horarios_ocupados(
            db, sessao.funcionario_id, candidatos[0], candidatos[-1], agora, ignorar_sessao_id=sessao.id
        )
        livres = [c for c in candidatos if not plano_service.comparar_conflitos([c], ocupados)]
    return HorariosDoDia(dia, livres, None if livres else DIA_SEM_HORARIOS)


def ultima_remarcacao(db: Session, sessao_id: int) -> HistoricoRemarcacao | None:
    return db.scalar(
        select(HistoricoRemarcacao)
        .where(HistoricoRemarcacao.sessao_id == sessao_id)
        .order_by(HistoricoRemarcacao.id.desc())
        .limit(1)
    )


def _atualizar_datas_do_plano(db: Session, plano_id: int) -> None:
    primeira, ultima = db.execute(
        select(func.min(Sessao.data_hora), func.max(Sessao.data_hora)).where(Sessao.plano_id == plano_id)
    ).one()
    plano = db.get(Plano, plano_id)
    plano.data_inicio, plano.data_fim = primeira.date(), ultima.date()


def remarcar(
    db: Session, cliente_id: int, sessao_id: int, novo: datetime, agora: datetime, agora_utc: datetime,
) -> ResultadoRemarcacao:
    try:
        resumo = buscar_sessao_do_cliente(db, cliente_id, sessao_id)
        plano_service.travar_agenda(db, resumo.funcionario_id)
        sessao = db.scalar(select(Sessao).where(Sessao.id == sessao_id).execution_options(populate_existing=True))
        original = sessao.data_hora

        ultima = ultima_remarcacao(db, sessao_id)
        if novo == original and ultima is not None and ultima.data_hora_nova == novo:
            db.rollback()
            return ResultadoRemarcacao(ultima.data_hora_original, novo, repetido=True)

        validar_sessao_remarcavel(sessao.status, original, agora)
        validar_novo_horario(original, novo, agora)
        if plano_service.verificar_conflitos(db, resumo.funcionario_id, [novo], agora, ignorar_sessao_id=sessao_id):
            raise ConflitoRemarcacao(MSG_CONFLITO)  # RN01

        sessao.data_hora = novo
        sessao.foi_remarcada = True
        db.add(HistoricoRemarcacao(
            sessao_id=sessao_id, data_hora_original=original, data_hora_nova=novo, solicitado_em=agora_utc,
        ))
        db.flush()
        _atualizar_datas_do_plano(db, sessao.plano_id)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return ResultadoRemarcacao(original, novo, repetido=False)
