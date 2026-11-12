from dataclasses import dataclass
from datetime import datetime, time, timedelta

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.core.templating import NOMES_DIAS, NOMES_MESES, juntar_nomes, tempo_curto
from app.models.paciente import Paciente
from app.models.pagamento import FormaPagamento
from app.models.sessao import StatusSessao
from app.services import inicio_service, plano_service
from app.services.contratacao_service import prazo_previsto
from app.services.inicio_service import AGUARDANDO_PAGAMENTO, ENCERRADO, PrimeirosPassos, ProximaSessao, ResumoPlano
from app.services.remarcacao_service import motivo_bloqueio

LIMITE_PRAZO_COM_HORA = timedelta(hours=24)


@dataclass(frozen=True)
class PagamentoPendente:
    resumo: ResumoPlano
    prazo: datetime
    texto: str


@dataclass(frozen=True)
class GrupoDoAtendido:
    nome: str
    planos: list[ResumoPlano]


@dataclass(frozen=True)
class InicioCliente:
    tem_atendido: bool
    primeiros_passos: PrimeirosPassos | None
    pagamentos: list[PagamentoPendente]
    proxima: ProximaSessao | None
    pode_remarcar: bool
    grupos: list[GrupoDoAtendido]
    encerrados: list[ResumoPlano]
    configuracoes: dict[int, str]


# --- Funções puras (RNF14: `agora` por parâmetro) --------------------------------------------------

def prazo_do_plano(resumo: ResumoPlano, agora: datetime) -> datetime:
    plano = resumo.plano
    if resumo.forma is None:  # sem cobrança escolhida: o prazo é o do resumo da contratação
        return prazo_previsto(plano.criado_em, plano.data_inicio, agora).pix
    return plano_service.prazo_pagamento(plano.criado_em, plano.data_inicio, resumo.forma)


def texto_prazo(prazo: datetime, boleto: bool, agora: datetime) -> str:
    restante = max(prazo - agora, timedelta(0))
    if restante < LIMITE_PRAZO_COM_HORA:
        dias = (prazo.date() - agora.date()).days
        dia = "" if dias <= 0 else "amanhã, " if dias == 1 else f"{prazo:%d/%m/%Y}, "
        return f"Pague até {dia}{prazo:%H:%M} (restam {tempo_curto(restante)})"
    return f"Pague até {prazo:%d/%m/%Y}" + (" (vencimento do boleto)" if boleto else "")


def quando_em_dias(data_hora: datetime, agora: datetime) -> str:
    dias = (data_hora.date() - agora.date()).days
    if dias <= 0:
        return "hoje"
    if dias == 1:
        return "amanhã"
    return f"em {dias} dias"


def data_por_extenso(data_hora: datetime) -> str:
    return f"{NOMES_DIAS[data_hora.weekday()]}, {data_hora.day} de {NOMES_MESES[data_hora.month - 1]}, às {data_hora:%H:%M}"


def configuracao_curta(frequencia: int, dias: list[int], horario: time) -> str:
    vezes = "1 vez por semana" if frequencia == 1 else f"{frequencia} vezes por semana"
    nomes = juntar_nomes([NOMES_DIAS[d].lower() + "s" for d in sorted(dias)]) if dias else ""
    return f"{nomes.capitalize()} às {horario:%H:%M}, {vezes}" if nomes else vezes.capitalize()


def pagamentos_pendentes(planos: list[ResumoPlano], agora: datetime) -> list[PagamentoPendente]:
    lista = []
    for resumo in planos:
        if resumo.situacao != AGUARDANDO_PAGAMENTO:
            continue
        prazo = prazo_do_plano(resumo, agora)
        lista.append(PagamentoPendente(resumo, prazo, texto_prazo(prazo, resumo.forma == FormaPagamento.BOLETO, agora)))
    return sorted(lista, key=lambda p: (p.prazo, p.resumo.plano.id))


def agrupar_por_atendido(planos: list[ResumoPlano]) -> tuple[list[GrupoDoAtendido], list[ResumoPlano]]:
    grupos: dict[str, list[ResumoPlano]] = {}
    for resumo in planos:
        if resumo.situacao != ENCERRADO:
            grupos.setdefault(resumo.paciente, []).append(resumo)
    encerrados = sorted(
        (r for r in planos if r.situacao == ENCERRADO),
        key=lambda r: (r.paciente.lower(), -r.plano.data_fim.toordinal(), r.plano.id),
    )
    ordenados = [GrupoDoAtendido(nome, grupos[nome]) for nome in sorted(grupos, key=lambda n: (n.lower(), n))]
    return ordenados, encerrados


# --- Montagem -----------------------------------------------------------------------------------------

def _tem_atendido(db: Session, cliente_id: int) -> bool:
    return db.scalar(select(exists().where(Paciente.cliente_id == cliente_id)))


def montar_inicio(db: Session, cliente_id: int, agora: datetime) -> InicioCliente:
    planos = inicio_service.planos_do_cliente(db, cliente_id, agora)
    tem_atendido = _tem_atendido(db, cliente_id)
    passos = inicio_service.primeiros_passos(tem_atendido, planos)
    pagamentos = pagamentos_pendentes(planos, agora)
    if passos is not None:  # antes do primeiro plano pago: só os passos e o bloco de pagamento
        return InicioCliente(tem_atendido, passos, pagamentos, None, False, [], [], {})
    proxima = inicio_service.proxima_sessao(db, cliente_id, agora)
    pode_remarcar = proxima is not None and motivo_bloqueio(StatusSessao.AGENDADA, proxima.data_hora, agora) is None
    grupos, encerrados = agrupar_por_atendido(planos)
    configuracoes = {
        r.plano.id: configuracao_curta(r.plano.frequencia_semanal, r.plano.dias_semana, r.plano.horario) for r in planos
    }
    return InicioCliente(tem_atendido, passos, pagamentos, proxima, pode_remarcar, grupos, encerrados, configuracoes)
