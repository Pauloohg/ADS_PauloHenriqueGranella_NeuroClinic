from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.core.templating import NOMES_DIAS
from app.models.cliente import Cliente
from app.models.especialidade import Especialidade
from app.models.funcionario import Funcionario
from app.models.pagamento import FormaPagamento
from app.models.paciente import Paciente
from app.models.plano import Plano, StatusPlano
from app.models.sessao import Sessao, StatusSessao
from app.models.usuario import Usuario
from app.schemas.cliente import validar_cpf
from app.services import cliente_service, funcionario_service, inicio_service, plano_service
from app.services.funcionario_service import FuncionarioResumo
from app.services.plano_service import (
    FREQUENCIAS,
    DiasSemanaInvalidos,
    HorarioInvalido,
    PlanoInvalido,
    SimulacaoPlano,
)

LIMITE_OBSERVACAO = 500

MSG_PACIENTE_OBRIGATORIO = "Selecione a pessoa que será atendida."
MSG_FREQUENCIA = "Escolha a frequência: 1, 2 ou 3 vezes por semana."
MSG_CPF_OBRIGATORIO = "Informe o seu CPF: ele é necessário para gerar a cobrança do plano."
MSG_CPF_EM_USO = "Este CPF já está cadastrado em outra conta."
MSG_OBSERVACAO = f"A observação deve ter no máximo {LIMITE_OBSERVACAO} caracteres."
MSG_CONFLITO = "Alguns horários escolhidos já estão ocupados para este profissional. Escolha outro dia ou horário."
MSG_RENOVACAO_INDISPONIVEL = (
    "A renovação fica disponível quando o plano for encerrado, depois da última sessão ({data_fim}) "
    "e com o pagamento resolvido."
)
MSG_PROFISSIONAL_INDISPONIVEL = (
    "{nome} não está mais disponível para novos planos de {especialidade}. Escolha outro profissional para renovar."
)
MSG_HORARIO_OCUPADO = (
    "O horário do plano anterior ({dias}, às {hora}) não está mais livre com {nome} em todo o novo período. "
    "Escolha um novo horário na grade ou outro profissional."
)
MSG_CONFIGURACAO_ANTIGA = "A configuração do plano anterior não é mais oferecida. Escolha os dias, o horário e a duração."


class PlanoNaoEncontrado(Exception):
    pass


class RenovacaoIndisponivel(Exception):
    pass


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


@dataclass(frozen=True)
class PrazoPrevisto:
    pix: datetime
    vencimento_boleto: date | None


def prazo_previsto(criado_em_utc: datetime, data_primeira_sessao: date, agora: datetime) -> PrazoPrevisto:
    pix = plano_service.prazo_pagamento(criado_em_utc, data_primeira_sessao, FormaPagamento.PIX)
    boleto = plano_service.prazo_pagamento(criado_em_utc, data_primeira_sessao, FormaPagamento.BOLETO).date()
    return PrazoPrevisto(pix, boleto if plano_service.boleto_disponivel(boleto, agora) else None)


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


# --- Confirmação (RF05) --------------------------------------------------------------------------

def _plano_reenviado(
    db: Session, paciente: Paciente | None, funcionario_id: int, celulas: list[str], frequencia: str, duracao: str,
    agora: datetime,
) -> Plano | None:
    if paciente is None:
        return None
    try:
        dias, horario = ler_celulas(celulas)
        simulacao = plano_service.simular_sessoes(dias, horario, _ler_frequencia(frequencia), duracao, agora)
    except PlanoInvalido:
        return None
    sessao_nao_pendente = exists().where(Sessao.plano_id == Plano.id, Sessao.status != StatusSessao.PENDENTE_PAGAMENTO)
    candidatos = db.scalars(
        select(Plano).where(
            Plano.paciente_id == paciente.id,
            Plano.funcionario_id == funcionario_id,
            Plano.horario == horario,
            Plano.data_inicio == simulacao.data_inicio,
            Plano.data_fim == simulacao.data_fim,
            plano_service.tem_sessao_pendente(),
            ~sessao_nao_pendente,
        )
    )
    for plano in candidatos:
        if sorted(plano.dias_semana) == sorted(dias) and plano_service.prazo_do_plano(db, plano) >= agora:  # RN09
            return plano
    return None


def _travar_agenda(db: Session, funcionario_id: int) -> None:
    # FOR UPDATE serializa confirmações simultâneas do mesmo profissional no PostgreSQL (o SQLite ignora)
    db.execute(select(Funcionario.usuario_id).where(Funcionario.usuario_id == funcionario_id).with_for_update())


def confirmar_plano(
    db: Session,
    cliente: Cliente,
    funcionario_id: int,
    paciente: Paciente | None,
    celulas: list[str],
    frequencia: str,
    duracao: str,
    observacao: str,
    agora: datetime,
    criado_em_utc: datetime,
) -> tuple[Plano, bool]:
    plano_service.remover_planos_vencidos(db, agora)
    try:
        _travar_agenda(db, funcionario_id)
        existente = _plano_reenviado(db, paciente, funcionario_id, celulas, frequencia, duracao, agora)
        if existente is not None:
            db.rollback()
            return existente, False

        resumo = preparar_resumo(
            db, cliente, funcionario_id, paciente, celulas, frequencia, duracao, observacao, "", agora
        )
        sim = resumo.simulacao
        plano = Plano(
            paciente_id=paciente.id,
            funcionario_id=funcionario_id,
            especialidade_id=sim.especialidade_id,
            dias_semana=resumo.dias_semana,
            horario=resumo.horario,
            frequencia_semanal=resumo.frequencia_semanal,
            duracao=resumo.duracao,
            valor_total=sim.valor_total,
            desconto_percentual=sim.desconto_percentual,
            valor_desconto=sim.valor_desconto,
            data_inicio=sim.data_inicio,
            data_fim=sim.data_fim,
            status=StatusPlano.ATIVO,
            observacao=resumo.observacao,
            criado_em=criado_em_utc,
        )
        db.add(plano)
        db.flush()
        db.add_all(
            Sessao(plano_id=plano.id, data_hora=data_hora, status=StatusSessao.PENDENTE_PAGAMENTO, foi_remarcada=False)
            for data_hora in sim.sessoes
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return plano, True


# --- Plano do Cliente ----------------------------------------------------------------------------

@dataclass(frozen=True)
class DetalhePlano:
    plano: Plano
    paciente: str
    especialidade: str
    profissional: str
    sessoes: list[datetime]
    situacao: str | None
    prazo_pagamento: datetime


def buscar_plano_do_cliente(db: Session, cliente_id: int, plano_id: int) -> Plano:
    plano = db.scalar(
        select(Plano)
        .join(Paciente, Paciente.id == Plano.paciente_id)
        .where(Plano.id == plano_id, Paciente.cliente_id == cliente_id)
    )
    if plano is None:
        raise PlanoNaoEncontrado
    return plano


def detalhar_plano(db: Session, cliente_id: int, plano_id: int, agora: datetime) -> DetalhePlano:
    plano = buscar_plano_do_cliente(db, cliente_id, plano_id)
    sessoes = db.execute(
        select(Sessao.data_hora, Sessao.status).where(Sessao.plano_id == plano.id).order_by(Sessao.data_hora)
    ).all()
    forma = plano_service.forma_pagamento_do_plano(db, plano.id)
    return DetalhePlano(
        plano=plano,
        paciente=db.get(Paciente, plano.paciente_id).nome,
        especialidade=db.get(Especialidade, plano.especialidade_id).nome,
        profissional=db.get(Usuario, plano.funcionario_id).nome,
        sessoes=[data_hora for data_hora, _ in sessoes],
        situacao=inicio_service.situacao_plano(
            plano.data_fim, Counter(status for _, status in sessoes), plano.criado_em, agora, plano.data_inicio, forma
        ),
        prazo_pagamento=plano_service.prazo_pagamento(plano.criado_em, plano.data_inicio, forma),
    )


# --- Renovação (HU07) ----------------------------------------------------------------------------

@dataclass(frozen=True)
class Renovacao:
    plano_anterior: Plano
    paciente: Paciente
    especialidade: Especialidade
    profissional_anterior: Usuario
    profissional: FuncionarioResumo | None
    outros_profissionais: list[Usuario]
    dados: dict
    simulacao: SimulacaoPlano | None
    aviso: str | None

    @property
    def mesma_configuracao_livre(self) -> bool:
        return self.simulacao is not None and not self.simulacao.conflitos


def descrever_dias(dias: list[int]) -> str:
    nomes = [NOMES_DIAS[dia] for dia in sorted(dias)]
    return nomes[0] if len(nomes) == 1 else ", ".join(nomes[:-1]) + " e " + nomes[-1]


def preparar_renovacao(
    db: Session, cliente_id: int, plano_id: int, agora: datetime, funcionario_id: int | None = None
) -> Renovacao:
    anterior = buscar_plano_do_cliente(db, cliente_id, plano_id)
    if not plano_service.plano_esta_encerrado(db, anterior, agora):
        raise RenovacaoIndisponivel(MSG_RENOVACAO_INDISPONIVEL.format(data_fim=anterior.data_fim.strftime("%d/%m/%Y")))

    especialidade = db.get(Especialidade, anterior.especialidade_id)
    ativos = funcionario_service.listar_ativos_da_especialidade(db, especialidade.id)
    escolhido_id = anterior.funcionario_id if funcionario_id is None else funcionario_id
    escolhido = next((usuario for usuario in ativos if usuario.id == escolhido_id), None)
    dados = {
        "paciente_id": str(anterior.paciente_id),
        "frequencia_semanal": str(anterior.frequencia_semanal),
        "duracao": anterior.duracao,
        "celulas": [f"{dia}-{anterior.horario.hour}" for dia in sorted(anterior.dias_semana)],
        "observacao": "",
    }

    profissional = simulacao = aviso = None
    if escolhido is None:
        nome = (db.get(Usuario, escolhido_id) or db.get(Usuario, anterior.funcionario_id)).nome
        aviso = MSG_PROFISSIONAL_INDISPONIVEL.format(nome=nome, especialidade=especialidade.nome)
    else:
        profissional = funcionario_service.buscar(db, escolhido.id)
        try:
            simulacao = plano_service.simular_plano(
                db, escolhido.id, anterior.dias_semana, anterior.horario, anterior.frequencia_semanal,
                anterior.duracao, agora,
            )
        except PlanoInvalido:
            aviso = MSG_CONFIGURACAO_ANTIGA
            dados["celulas"] = []
        if simulacao is not None and simulacao.conflitos:
            aviso = MSG_HORARIO_OCUPADO.format(
                dias=descrever_dias(anterior.dias_semana), hora=anterior.horario.strftime("%H:%M"), nome=escolhido.nome,
            )
            dados["celulas"] = []

    return Renovacao(
        plano_anterior=anterior,
        paciente=db.get(Paciente, anterior.paciente_id),
        especialidade=especialidade,
        profissional_anterior=db.get(Usuario, anterior.funcionario_id),
        profissional=profissional,
        outros_profissionais=[usuario for usuario in ativos if usuario.id != escolhido_id],
        dados=dados,
        simulacao=simulacao,
        aviso=aviso,
    )
