from dataclasses import dataclass, field
from datetime import datetime, time

from sqlalchemy.orm import Session

from app.models.cliente import Cliente
from app.models.especialidade import Especialidade
from app.models.paciente import Paciente
from app.models.usuario import Usuario
from app.schemas.cliente import validar_cpf
from app.services import cliente_service, plano_service
from app.services.plano_service import (
    FREQUENCIAS,
    DiasSemanaInvalidos,
    HorarioInvalido,
    PlanoInvalido,
    SimulacaoPlano,
)

LIMITE_OBSERVACAO = 500

MSG_PACIENTE_OBRIGATORIO = "Selecione o paciente que será atendido."
MSG_FREQUENCIA = "Escolha a frequência: 1, 2 ou 3 vezes por semana."
MSG_CPF_OBRIGATORIO = "Informe o seu CPF: ele é necessário para gerar a cobrança do plano."
MSG_CPF_EM_USO = "Este CPF já está cadastrado em outra conta."
MSG_OBSERVACAO = f"A observação deve ter no máximo {LIMITE_OBSERVACAO} caracteres."
MSG_CONFLITO = "Alguns horários escolhidos já estão ocupados para este profissional. Escolha outro dia ou horário."


class ContratacaoInvalida(Exception):
    def __init__(self, erros: dict[str, str], conflitos: list[datetime] | None = None):
        super().__init__(erros)
        self.erros = erros
        self.conflitos = conflitos or []


@dataclass(frozen=True)
class ResumoContratacao:
    simulacao: SimulacaoPlano
    paciente: Paciente
    profissional: Usuario
    especialidade: Especialidade
    dias_semana: list[int]
    horario: time
    frequencia_semanal: int
    duracao: str
    observacao: str | None
    cpf_registrado: bool = field(default=False)


def ler_celulas(celulas: list[str]) -> tuple[list[int], time]:
    pares = []
    for celula in celulas:
        dia, _, hora = celula.partition("-")
        if not (dia.isdigit() and hora.isdigit()):
            raise DiasSemanaInvalidos("Seleção de horário inválida. Escolha os horários na grade.")
        pares.append((int(dia), int(hora)))
    if not pares:
        raise DiasSemanaInvalidos("Escolha os horários das sessões na grade.")
    horas = {hora for _, hora in pares}
    if len(horas) > 1:
        raise HorarioInvalido("Todas as sessões do plano precisam ser no mesmo horário.")
    hora = horas.pop()
    if hora > 23:
        raise HorarioInvalido("Seleção de horário inválida. Escolha os horários na grade.")
    return [dia for dia, _ in pares], time(hora)


def _ler_frequencia(frequencia: str) -> int:
    if not frequencia.strip().isdigit() or int(frequencia) not in FREQUENCIAS:
        raise plano_service.FrequenciaInvalida(MSG_FREQUENCIA)
    return int(frequencia)


def _validar_cpf_inline(db: Session, cliente: Cliente, cpf: str) -> str:
    try:
        cpf_formatado = validar_cpf(cpf)
    except ValueError as exc:
        raise ContratacaoInvalida({"cpf": str(exc)}) from None
    if cpf_formatado is None:
        raise ContratacaoInvalida({"cpf": MSG_CPF_OBRIGATORIO})
    if cliente_service.cpf_em_uso(db, cliente.usuario_id, cpf_formatado):
        raise ContratacaoInvalida({"cpf": MSG_CPF_EM_USO})
    return cpf_formatado


def preparar_resumo(
    db: Session,
    cliente: Cliente,
    funcionario_id: int,
    paciente: Paciente | None,
    celulas: list[str],
    frequencia: str,
    duracao: str,
    observacao: str,
    cpf: str,
    agora: datetime,
) -> ResumoContratacao:
    erros: dict[str, str] = {}
    cpf_novo = None
    if cliente.cpf is None:
        try:
            cpf_novo = _validar_cpf_inline(db, cliente, cpf)
        except ContratacaoInvalida as exc:
            erros.update(exc.erros)
    if paciente is None:
        erros["paciente_id"] = MSG_PACIENTE_OBRIGATORIO
    observacao = observacao.strip() or None
    if observacao and len(observacao) > LIMITE_OBSERVACAO:
        erros["observacao"] = MSG_OBSERVACAO

    frequencia_semanal = dias = horario = None
    try:
        frequencia_semanal = _ler_frequencia(frequencia)
    except PlanoInvalido as exc:
        erros["frequencia_semanal"] = str(exc)
    try:
        plano_service.validar_duracao(duracao)
    except PlanoInvalido as exc:
        erros["duracao"] = str(exc)
    try:
        dias, horario = ler_celulas(celulas)
    except PlanoInvalido as exc:
        erros["celulas"] = str(exc)

    simulacao = None
    if not {"frequencia_semanal", "duracao", "celulas"} & erros.keys():
        try:
            simulacao = plano_service.simular_plano(
                db, funcionario_id, dias, horario, frequencia_semanal, duracao, agora
            )
        except PlanoInvalido as exc:
            erros["celulas"] = str(exc)
    if simulacao is not None and simulacao.conflitos:
        erros["celulas"] = MSG_CONFLITO
    if erros:
        raise ContratacaoInvalida(erros, simulacao.conflitos if simulacao else [])

    if cpf_novo is not None:
        cliente.cpf = cpf_novo
        db.commit()
    return ResumoContratacao(
        simulacao=simulacao,
        paciente=paciente,
        profissional=db.get(Usuario, funcionario_id),
        especialidade=db.get(Especialidade, simulacao.especialidade_id),
        dias_semana=sorted(dias),
        horario=horario,
        frequencia_semanal=frequencia_semanal,
        duracao=duracao,
        observacao=observacao,
        cpf_registrado=cpf_novo is not None,
    )
