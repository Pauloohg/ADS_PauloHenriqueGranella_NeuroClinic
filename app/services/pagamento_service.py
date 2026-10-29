import base64
import hashlib
import hmac
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from fastapi import BackgroundTasks
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import (
    ABACATEPAY_CHAVE_PUBLICA_HMAC,
    INTERVALO_CONFERENCIA_SEGUNDOS,
    MARGEM_EXPIRACAO_PIX_SEGUNDOS,
    MINIMO_RESTANTE_PIX_SEGUNDOS,
    TIMEOUT_CONFERENCIA,
    settings,
)
from app.core.email import enviar_email
from app.core.fuso import utc_para_local
from app.core.templating import moeda, templates
from app.models.cliente import Cliente
from app.models.especialidade import Especialidade
from app.models.paciente import Paciente
from app.models.pagamento import FormaPagamento, Pagamento, StatusPagamento
from app.models.plano import Plano
from app.models.sessao import Sessao, StatusSessao
from app.models.usuario import Usuario
from app.services import abacatepay_client, plano_service
from app.services.abacatepay_client import Cobranca, ErroAbacatePay
from app.services.contratacao_service import buscar_plano_do_cliente

logger = logging.getLogger(__name__)

EVENTO_PAGAMENTO_CONFIRMADO = "transparent.completed"
STATUS_COBRANCA_PAGA = "PAID"
INTERVALO_CONFERENCIA = timedelta(seconds=INTERVALO_CONFERENCIA_SEGUNDOS)
PROFUNDIDADE_CHAVES = 4  # chega a data.payerInformation.<MÉTODO>.<campo>
ASSUNTO_CONFIRMACAO = "Pagamento confirmado — plano de atendimento"
METODO_POR_FORMA = {FormaPagamento.PIX: abacatepay_client.PIX, FormaPagamento.BOLETO: abacatepay_client.BOLETO}
ROTULOS_FORMA = {FormaPagamento.PIX: "Pix", FormaPagamento.BOLETO: "Boleto"}

MSG_FORMA_INVALIDA = "Escolha a forma de pagamento: Pix ou boleto."
MSG_PLANO_PAGO = "Este plano já está pago."
MSG_NAO_AGUARDANDO = "Este plano não está aguardando pagamento."
MSG_PRAZO_ENCERRADO = (
    "O prazo para pagar este plano terminou em {prazo} (horário de Brasília). Os horários foram liberados; "
    "contrate um novo plano quando quiser."
)
MSG_PIX_SEM_TEMPO = (
    "Faltam menos de 2 minutos para o fim do prazo de pagamento: não há tempo para gerar o Pix. "
    "Contrate um novo plano quando quiser."
)
MSG_BOLETO_INDISPONIVEL = "O boleto não está disponível para este plano porque a primeira sessão é muito próxima."
MSG_CPF = "Cadastre o seu CPF em Meu perfil para gerar a cobrança."
MSG_CELULAR = "Cadastre um telefone celular com DDD em Meu perfil para gerar a cobrança."
MSG_COBRANCA_INDISPONIVEL = (
    "Não foi possível carregar os dados da cobrança agora. Atualize a página em alguns instantes."
)

# Cobranças pendentes já lidas da API; a tela de cobrança se atualiza a cada 15 s e não precisa consultar a API toda vez
_COBRANCAS: dict[str, Cobranca] = {}
_CONFERENCIAS: dict[str, datetime] = {}
LIMITE_CACHE = 500


class PagamentoIndisponivel(Exception):
    pass


class FormaPagamentoInvalida(Exception):
    pass


class CobrancaNaoGerada(Exception):
    pass


@dataclass(frozen=True)
class TelaCobranca:
    plano: Plano
    pagamento: Pagamento
    cobranca: Cobranca | None
    prazo: datetime
    no_prazo: bool
    erro: str | None

    @property
    def confirmado(self) -> bool:
        return self.pagamento.status == StatusPagamento.CONFIRMADO


@dataclass(frozen=True)
class ResultadoWebhook:
    situacao: str
    pagamento_id: int | None = None


# --- Regras ---------------------------------------------------------------------------------------

def ler_forma(valor: str) -> FormaPagamento:
    try:
        return FormaPagamento(valor.strip().lower())
    except ValueError:
        raise FormaPagamentoInvalida(MSG_FORMA_INVALIDA) from None


def buscar_pagamento(db: Session, plano_id: int) -> Pagamento | None:
    return db.scalar(select(Pagamento).where(Pagamento.plano_id == plano_id))


def _tem_sessao_pendente(db: Session, plano_id: int) -> bool:
    consulta = select(Sessao.id).where(Sessao.plano_id == plano_id, Sessao.status == StatusSessao.PENDENTE_PAGAMENTO)
    return db.scalar(consulta.limit(1)) is not None


def _formatar_prazo(prazo: datetime) -> str:
    return prazo.strftime("%d/%m/%Y às %H:%M")


def verificar_pagavel(db: Session, plano: Plano, agora: datetime) -> None:
    pagamento = buscar_pagamento(db, plano.id)
    if pagamento is not None and pagamento.status == StatusPagamento.CONFIRMADO:
        raise PagamentoIndisponivel(MSG_PLANO_PAGO)
    if not _tem_sessao_pendente(db, plano.id):
        raise PagamentoIndisponivel(MSG_NAO_AGUARDANDO)
    # Sem forma escolhida o prazo é o de 4 h: é a janela para gerar a cobrança, inclusive a do boleto
    prazo = plano_service.prazo_pagamento(plano.criado_em, plano.data_inicio, None)
    if prazo < agora:  # RN09
        raise PagamentoIndisponivel(MSG_PRAZO_ENCERRADO.format(prazo=_formatar_prazo(prazo)))


def segundos_expiracao_pix(plano_criado_em_utc: datetime, agora: datetime) -> int:
    restante = int((plano_service.prazo_pagamento(plano_criado_em_utc, forma=FormaPagamento.PIX) - agora).total_seconds())
    if restante < MINIMO_RESTANTE_PIX_SEGUNDOS:
        raise CobrancaNaoGerada(MSG_PIX_SEM_TEMPO)
    return restante - MARGEM_EXPIRACAO_PIX_SEGUNDOS


def vencimento_boleto_do_plano(plano: Plano) -> date:
    return plano_service.vencimento_boleto(utc_para_local(plano.criado_em).date(), plano.data_inicio)


def boleto_oferecido(plano: Plano, agora: datetime) -> date | None:
    vencimento = vencimento_boleto_do_plano(plano)
    return vencimento if plano_service.boleto_disponivel(vencimento, agora) else None


def formatar_celular(telefone: str | None) -> str:
    digitos = "".join(c for c in telefone or "" if c.isdigit())
    if len(digitos) in (12, 13) and digitos.startswith("55"):
        digitos = digitos[2:]
    if len(digitos) == 11:
        return f"({digitos[:2]}) {digitos[2:7]}-{digitos[7:]}"
    if len(digitos) == 10:
        return f"({digitos[:2]}) {digitos[2:6]}-{digitos[6:]}"
    raise CobrancaNaoGerada(MSG_CELULAR)


def dados_pagador(usuario: Usuario, cliente: Cliente) -> dict:
    cpf = "".join(c for c in cliente.cpf or "" if c.isdigit())
    if len(cpf) != 11:
        raise CobrancaNaoGerada(MSG_CPF)
    return {"name": usuario.nome, "email": usuario.email, "taxId": cpf, "cellphone": formatar_celular(cliente.telefone)}


def _descricao(db: Session, plano: Plano) -> str:
    especialidade = db.get(Especialidade, plano.especialidade_id).nome
    paciente = db.get(Paciente, plano.paciente_id).nome
    return abacatepay_client.sanear_texto(f"NeuroClinic - plano de {especialidade} ({paciente})")


def _guardar(cobranca: Cobranca) -> None:
    if len(_COBRANCAS) >= LIMITE_CACHE:
        _COBRANCAS.clear()
    _COBRANCAS[cobranca.id] = cobranca


def _registrar_conferencia(cobranca_id: str, agora: datetime) -> None:
    if len(_CONFERENCIAS) >= LIMITE_CACHE:
        _CONFERENCIAS.clear()
    _CONFERENCIAS[cobranca_id] = agora


def conferencia_liberada(cobranca_id: str, agora: datetime) -> bool:
    ultima = _CONFERENCIAS.get(cobranca_id)
    return ultima is None or agora - ultima >= INTERVALO_CONFERENCIA


# --- Cobrança (Cliente) ---------------------------------------------------------------------------

def iniciar_pagamento(
    db: Session, usuario: Usuario, plano_id: int, forma_texto: str, agora: datetime
) -> Pagamento:
    plano = buscar_plano_do_cliente(db, usuario.id, plano_id)
    existente = buscar_pagamento(db, plano.id)
    if existente is not None:
        return existente
    verificar_pagavel(db, plano, agora)
    forma = ler_forma(forma_texto)
    vencimento = None
    if forma == FormaPagamento.BOLETO:
        vencimento = boleto_oferecido(plano, agora)
        if vencimento is None:  # RN09: a tela não oferece, mas o servidor não confia nela
            raise FormaPagamentoInvalida(MSG_BOLETO_INDISPONIVEL)

    pagador = dados_pagador(usuario, db.get(Cliente, usuario.id))
    expira_em = segundos_expiracao_pix(plano.criado_em, agora) if forma == FormaPagamento.PIX else None
    try:
        cobranca = abacatepay_client.criar_cobranca(
            METODO_POR_FORMA[forma],
            abacatepay_client.centavos(plano.valor_total),
            pagador,
            plano.id,
            _descricao(db, plano),
            expira_em_segundos=expira_em,
            vencimento=vencimento,
        )
    except ErroAbacatePay as exc:
        raise CobrancaNaoGerada(exc.mensagem) from None

    pagamento = Pagamento(
        plano_id=plano.id, forma_pagamento=forma, status=StatusPagamento.PENDENTE, abacatepay_payment_id=cobranca.id
    )
    db.add(pagamento)
    try:
        db.commit()
    except IntegrityError:
        # Duplo envio: o outro pedido gravou primeiro; a cobrança recém-criada fica órfã na AbacatePay e expira sozinha
        db.rollback()
        logger.warning("Cobrança %s descartada: o plano %s já tinha pagamento", cobranca.id, plano.id)
        return buscar_pagamento(db, plano.id)
    _guardar(cobranca)
    _registrar_conferencia(cobranca.id, agora)
    return pagamento


def conferir_pagamento(
    db: Session, plano: Plano, pagamento: Pagamento, agora: datetime, agora_utc: datetime,
    tarefas: BackgroundTasks | None = None,
) -> Cobranca | None:
    cobranca_id = pagamento.abacatepay_payment_id
    if pagamento.status != StatusPagamento.PENDENTE or not cobranca_id or not conferencia_liberada(cobranca_id, agora):
        return None
    _registrar_conferencia(cobranca_id, agora)
    try:
        cobranca = abacatepay_client.consultar_cobranca(cobranca_id, timeout=TIMEOUT_CONFERENCIA)
    except ErroAbacatePay:
        logger.warning("Conferência do pagamento %s não concluída: AbacatePay indisponível", pagamento.id)
        return None
    _guardar(cobranca)
    if cobranca.status != STATUS_COBRANCA_PAGA:
        return cobranca
    try:
        resultado = confirmar_pagamento(db, plano, pagamento, agora, agora_utc, tarefas, cobranca.valor_centavos)
    except Exception as exc:
        logger.warning("Conferência do pagamento %s falhou ao confirmar (%s)", pagamento.id, type(exc).__name__)
        return cobranca
    if resultado.situacao == "confirmado":
        logger.warning(
            "Pagamento %s confirmado por conferência, webhook não chegou (cobrança %s)", pagamento.id, cobranca_id
        )
    return cobranca


def montar_tela_cobranca(
    db: Session, cliente_id: int, plano_id: int, agora: datetime, agora_utc: datetime | None = None,
    tarefas: BackgroundTasks | None = None,
) -> TelaCobranca | None:
    plano = buscar_plano_do_cliente(db, cliente_id, plano_id)
    pagamento = buscar_pagamento(db, plano.id)
    if pagamento is None:
        return None
    prazo = plano_service.prazo_pagamento(plano.criado_em, plano.data_inicio, pagamento.forma_pagamento)
    no_prazo = prazo >= agora
    cobranca = erro = None
    if agora_utc is not None:
        cobranca = conferir_pagamento(db, plano, pagamento, agora, agora_utc, tarefas)
    if pagamento.status == StatusPagamento.PENDENTE and no_prazo:
        cobranca = cobranca or _COBRANCAS.get(pagamento.abacatepay_payment_id)
        if cobranca is None:
            erro = MSG_COBRANCA_INDISPONIVEL
    return TelaCobranca(plano, pagamento, cobranca, prazo, no_prazo, erro)


# --- Webhook --------------------------------------------------------------------------------------

def secret_valido(recebido: str | None) -> bool:
    esperado = settings.ABACATEPAY_WEBHOOK_SECRET
    if not esperado or recebido is None:
        return False
    return hmac.compare_digest(recebido.encode(), esperado.encode())


def calcular_assinatura(corpo_bruto: bytes) -> str:
    digest = hmac.new(ABACATEPAY_CHAVE_PUBLICA_HMAC.encode(), corpo_bruto, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def assinatura_valida(corpo_bruto: bytes, assinatura: str | None) -> bool:
    if not assinatura:
        return False
    return hmac.compare_digest(calcular_assinatura(corpo_bruto).encode(), assinatura.strip().encode())


def _dados_cobranca(evento: dict) -> tuple[str | None, object]:
    dados = evento.get("data") if isinstance(evento.get("data"), dict) else {}
    transparente = dados.get("transparent") if isinstance(dados.get("transparent"), dict) else {}
    return transparente.get("id"), transparente.get("amount")


def _valor_reais(valor_centavos) -> str:
    try:
        return moeda(Decimal(int(valor_centavos)) / 100)
    except (TypeError, ValueError):
        return "valor desconhecido"


def _confirmar(db: Session, pagamento: Pagamento, agora_utc: datetime) -> bool:
    # UPDATE condicional: com webhook e conferência ao mesmo tempo, só um deles muda a linha (e manda o e-mail)
    alterado = db.execute(
        update(Pagamento)
        .where(Pagamento.id == pagamento.id, Pagamento.status == StatusPagamento.PENDENTE)
        .values(status=StatusPagamento.CONFIRMADO, confirmado_em=agora_utc)
        .execution_options(synchronize_session=False)
    ).rowcount
    if not alterado:
        db.rollback()
        db.refresh(pagamento)
        return False
    db.execute(
        update(Sessao)
        .where(Sessao.plano_id == pagamento.plano_id, Sessao.status == StatusSessao.PENDENTE_PAGAMENTO)
        .values(status=StatusSessao.AGENDADA)
    )
    db.commit()
    db.refresh(pagamento)
    return True


def _descrever_chaves(valor, profundidade: int = PROFUNDIDADE_CHAVES) -> str:
    if isinstance(valor, list):
        dicionarios = [item for item in valor if isinstance(item, dict)]
        return "[" + (_descrever_chaves(dicionarios[0], profundidade) if dicionarios else "") + "]"
    if not isinstance(valor, dict) or profundidade == 0:
        return ""
    partes = []
    for chave in sorted(valor, key=str):
        nome = "".join(c for c in str(chave)[:40] if c.isprintable())
        partes.append(nome + _descrever_chaves(valor[chave], profundidade - 1))
    return "{" + ", ".join(partes) + "}"


def confirmar_pagamento(
    db: Session, plano: Plano, pagamento: Pagamento, agora: datetime, agora_utc: datetime,
    tarefas: BackgroundTasks | None = None, valor_centavos=None,
) -> ResultadoWebhook:
    cobranca_id = pagamento.abacatepay_payment_id
    if pagamento.status == StatusPagamento.CONFIRMADO:
        return ResultadoWebhook("ja_confirmado", pagamento.id)
    try:
        if plano_service.prazo_pagamento(plano.criado_em, plano.data_inicio, pagamento.forma_pagamento) < agora:  # RN09
            plano_service.travar_agenda(db, plano.funcionario_id)
            datas = list(db.scalars(
                select(Sessao.data_hora)
                .where(Sessao.plano_id == plano.id, Sessao.status == StatusSessao.PENDENTE_PAGAMENTO)
            ))
            if plano_service.verificar_conflitos(db, plano.funcionario_id, datas, agora):  # RN01
                db.rollback()
                logger.error(
                    "Pagamento fora do prazo com conflito: estorno manual (cobrança %s, %s, plano %s)",
                    cobranca_id, _valor_reais(valor_centavos), plano.id,
                )
                return ResultadoWebhook("conflito", pagamento.id)
        if not _confirmar(db, pagamento, agora_utc):
            return ResultadoWebhook("ja_confirmado", pagamento.id)
    except Exception:
        db.rollback()
        raise

    if tarefas is not None:
        destinatario, corpo = montar_email_confirmacao(db, pagamento)
        tarefas.add_task(enviar_email, destinatario, ASSUNTO_CONFIRMACAO, corpo)
    return ResultadoWebhook("confirmado", pagamento.id)


def processar_evento(
    db: Session, evento: dict, agora: datetime, agora_utc: datetime, tarefas: BackgroundTasks | None = None
) -> ResultadoWebhook:
    if evento.get("event") != EVENTO_PAGAMENTO_CONFIRMADO:
        return ResultadoWebhook("ignorado")
    cobranca_id, valor = _dados_cobranca(evento)
    if not cobranca_id:
        # Só os nomes das chaves: o corpo tem nome, e-mail e CPF do pagador
        logger.warning(
            "Webhook %s sem data.transparent.id; nada foi confirmado. Chaves recebidas: %s",
            EVENTO_PAGAMENTO_CONFIRMADO, _descrever_chaves(evento),
        )

    pagamento = db.scalar(select(Pagamento).where(Pagamento.abacatepay_payment_id == cobranca_id)) if cobranca_id else None
    plano = db.get(Plano, pagamento.plano_id) if pagamento is not None else None
    if plano is None:
        logger.error(
            "Pagamento sem plano correspondente: estorno manual (cobrança %s, %s)", cobranca_id, _valor_reais(valor)
        )
        return ResultadoWebhook("desconhecido")
    return confirmar_pagamento(db, plano, pagamento, agora, agora_utc, tarefas, valor)


# --- E-mail de confirmação (RF06) -----------------------------------------------------------------

def montar_email_confirmacao(db: Session, pagamento: Pagamento) -> tuple[str, str]:
    plano = db.get(Plano, pagamento.plano_id)
    paciente = db.get(Paciente, plano.paciente_id)
    responsavel = db.get(Usuario, paciente.cliente_id)
    sessoes = list(db.scalars(select(Sessao.data_hora).where(Sessao.plano_id == plano.id).order_by(Sessao.data_hora)))
    corpo = templates.get_template("email/pagamento_confirmado.html").render(
        nome=responsavel.nome,
        plano=plano,
        paciente=paciente.nome,
        especialidade=db.get(Especialidade, plano.especialidade_id).nome,
        profissional=db.get(Usuario, plano.funcionario_id).nome,
        forma=ROTULOS_FORMA.get(pagamento.forma_pagamento, "—"),
        confirmado_em=utc_para_local(pagamento.confirmado_em),
        sessoes=sessoes,
    )
    return responsavel.email, corpo


def data_confirmacao_local(pagamento: Pagamento | None) -> datetime | None:
    if pagamento is None or pagamento.confirmado_em is None:
        return None
    return utc_para_local(pagamento.confirmado_em)
