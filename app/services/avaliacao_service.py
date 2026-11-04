import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import BinaryIO

from fastapi import BackgroundTasks
from sqlalchemy import exists, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import RAIZ_PROJETO, TAMANHO_MAXIMO_AVALIACAO_MB, settings
from app.core.email import enviar_email
from app.core.templating import templates
from app.models.avaliacao import Avaliacao
from app.models.especialidade import Especialidade
from app.models.paciente import Paciente
from app.models.plano import Plano
from app.models.sessao import Sessao, StatusSessao
from app.models.usuario import TipoUsuario, Usuario
from app.services.admin_clientes_service import POR_PAGINA, calcular_idade
from app.services.plano_service import DURACAO_SESSAO

logger = logging.getLogger(__name__)

TAMANHO_MAXIMO_BYTES = TAMANHO_MAXIMO_AVALIACAO_MB * 1024 * 1024
TAMANHO_BLOCO = 64 * 1024
ASSINATURA_PDF = b"%PDF-"
NOME_ARQUIVO_VALIDO = re.compile(r"[0-9a-f]{32}\.pdf")
ASSUNTO_NOVA_AVALIACAO = "Nova avaliação disponível"

MSG_ANTES_DO_TERMINO = "Só é possível registrar depois do fim da sessão ({fim})."
MSG_PENDENTE = "Esta consulta ainda aguarda o pagamento do plano e não pode ser registrada."
MSG_JA_REGISTRADA = "Esta consulta já foi registrada."
MSG_SEM_ARQUIVO = "Escolha o arquivo PDF da avaliação."
MSG_NAO_PDF = "Envie a avaliação em PDF (arquivo terminado em .pdf)."
MSG_PDF_INVALIDO = "O arquivo enviado não é um PDF válido. Gere o PDF novamente e tente outra vez."
MSG_VAZIO = "O arquivo enviado está vazio."
MSG_GRANDE = f"O arquivo passa do limite de {TAMANHO_MAXIMO_AVALIACAO_MB} MB. Reduza o tamanho do PDF e tente novamente."


class RegistroInvalido(Exception):
    def __init__(self, mensagem: str):
        super().__init__(mensagem)
        self.mensagem = mensagem


class RegistroBloqueado(RegistroInvalido):
    pass


class JaRegistrada(RegistroBloqueado):
    def __init__(self):
        super().__init__(MSG_JA_REGISTRADA)


class ArquivoInvalido(RegistroInvalido):
    pass


class SessaoNaoEncontrada(Exception):
    pass


class AvaliacaoNaoEncontrada(Exception):
    pass


@dataclass(frozen=True)
class SessaoParaRegistro:
    id: int
    data_hora: datetime
    status: StatusSessao
    foi_remarcada: bool
    atendido: str
    idade: int
    especialidade: str

    @property
    def fim(self) -> datetime:
        return termino(self.data_hora)


@dataclass(frozen=True)
class PaginaRegistro:
    consultas: list[SessaoParaRegistro]
    pagina: int
    total_paginas: int
    total: int


@dataclass(frozen=True)
class ArquivoAvaliacao:
    avaliacao_id: int
    nome: str
    data_sessao: date


# --- Quando a sessão pode ser registrada (DECISÃO 1) ---------------------------------------------

def termino(data_hora: datetime) -> datetime:
    return data_hora + DURACAO_SESSAO


def motivo_bloqueio(status: StatusSessao, data_hora: datetime, agora: datetime) -> str | None:
    if status == StatusSessao.REALIZADA:
        return MSG_JA_REGISTRADA
    if status != StatusSessao.AGENDADA:
        return MSG_PENDENTE
    fim = termino(data_hora)
    if agora < fim:
        return MSG_ANTES_DO_TERMINO.format(fim=f"{fim:%d/%m/%Y} às {fim:%H:%M}")
    return None


def pode_registrar(status: StatusSessao, data_hora: datetime, agora: datetime) -> bool:
    return motivo_bloqueio(status, data_hora, agora) is None


def validar_registravel(status: StatusSessao, data_hora: datetime, agora: datetime) -> None:
    if status == StatusSessao.REALIZADA:
        raise JaRegistrada
    motivo = motivo_bloqueio(status, data_hora, agora)
    if motivo:
        raise RegistroBloqueado(motivo)


# --- Consultas ao banco --------------------------------------------------------------------------

def _consulta_do_profissional(funcionario_id: int):
    return (
        select(
            Sessao.id, Sessao.data_hora, Sessao.status, Sessao.foi_remarcada,
            Paciente.nome, Paciente.data_nascimento, Especialidade.nome,
        )
        .select_from(Sessao)
        .join(Plano, Plano.id == Sessao.plano_id)
        .join(Paciente, Paciente.id == Plano.paciente_id)
        .join(Especialidade, Especialidade.id == Plano.especialidade_id)
        .where(Plano.funcionario_id == funcionario_id)
    )


def _montar(linha, hoje: date) -> SessaoParaRegistro:
    sessao_id, data_hora, status, remarcada, nome, nascimento, especialidade = linha
    return SessaoParaRegistro(
        sessao_id, data_hora, status, remarcada, nome, calcular_idade(nascimento, hoje), especialidade,
    )


def buscar_sessao_do_profissional(db: Session, funcionario_id: int, sessao_id: int, hoje: date) -> SessaoParaRegistro:
    linha = db.execute(_consulta_do_profissional(funcionario_id).where(Sessao.id == sessao_id)).first()
    if linha is None:
        raise SessaoNaoEncontrada
    return _montar(linha, hoje)


def _filtro_a_registrar(funcionario_id: int, agora: datetime):
    return (
        Plano.funcionario_id == funcionario_id,
        Sessao.status == StatusSessao.AGENDADA,
        Sessao.data_hora <= agora - DURACAO_SESSAO,
    )


def contar_a_registrar(db: Session, funcionario_id: int, agora: datetime) -> int:
    consulta = (
        select(func.count(Sessao.id))
        .join(Plano, Plano.id == Sessao.plano_id)
        .where(*_filtro_a_registrar(funcionario_id, agora))
    )
    return db.scalar(consulta)


def consultas_a_registrar(db: Session, funcionario_id: int, agora: datetime, pagina: int = 1) -> PaginaRegistro:
    total = contar_a_registrar(db, funcionario_id, agora)
    total_paginas = max(1, -(-total // POR_PAGINA))
    pagina = min(max(1, pagina), total_paginas)
    consulta = (
        _consulta_do_profissional(funcionario_id)
        .where(*_filtro_a_registrar(funcionario_id, agora))
        .order_by(Sessao.data_hora, Sessao.id)
        .limit(POR_PAGINA)
        .offset((pagina - 1) * POR_PAGINA)
    )
    hoje = agora.date()
    return PaginaRegistro([_montar(linha, hoje) for linha in db.execute(consulta)], pagina, total_paginas, total)


# --- Arquivo da avaliação (DECISÃO 2) ------------------------------------------------------------

def pasta_avaliacoes() -> Path:
    pasta = Path(settings.AVALIACOES_DIR)
    return (pasta if pasta.is_absolute() else RAIZ_PROJETO / pasta).resolve()


def caminho_do_arquivo(nome: str) -> Path:
    if not NOME_ARQUIVO_VALIDO.fullmatch(nome or ""):
        raise AvaliacaoNaoEncontrada
    pasta = pasta_avaliacoes()
    caminho = (pasta / nome).resolve()
    if caminho.parent != pasta:
        raise AvaliacaoNaoEncontrada
    return caminho


def salvar_arquivo(arquivo: BinaryIO, nome_enviado: str | None) -> str:
    if not nome_enviado:
        raise ArquivoInvalido(MSG_SEM_ARQUIVO)
    if Path(nome_enviado.replace("\\", "/")).suffix.lower() != ".pdf":
        raise ArquivoInvalido(MSG_NAO_PDF)

    primeiro = arquivo.read(TAMANHO_BLOCO)
    if not primeiro:
        raise ArquivoInvalido(MSG_VAZIO)
    if not primeiro.startswith(ASSINATURA_PDF):
        raise ArquivoInvalido(MSG_PDF_INVALIDO)

    pasta = pasta_avaliacoes()
    pasta.mkdir(parents=True, exist_ok=True)
    nome = f"{uuid.uuid4().hex}.pdf"
    destino = pasta / nome
    parcial = pasta / f"{nome}.parcial"
    try:
        tamanho = 0
        with open(parcial, "xb") as saida:
            bloco = primeiro
            while bloco:
                tamanho += len(bloco)
                if tamanho > TAMANHO_MAXIMO_BYTES:
                    raise ArquivoInvalido(MSG_GRANDE)
                saida.write(bloco)
                bloco = arquivo.read(TAMANHO_BLOCO)
        os.replace(parcial, destino)
    except BaseException:
        parcial.unlink(missing_ok=True)
        raise
    return nome


def apagar_arquivo(nome: str | None) -> None:
    if not nome:
        return
    try:
        caminho_do_arquivo(nome).unlink(missing_ok=True)
    except (AvaliacaoNaoEncontrada, OSError):
        logger.warning("Não foi possível apagar um arquivo de avaliação descartado")


# --- Registro do atendimento (HU11 e HU12) -------------------------------------------------------

def _concluir_sessao(
    db: Session, sessao_id: int, agora: datetime, agora_utc: datetime, comparecimento: bool, arquivo: str | None,
) -> None:
    # RN04: a avaliação só nasce junto da mudança para realizada, e o WHERE repete a DECISÃO 1 de forma atômica
    resultado = db.execute(
        update(Sessao)
        .where(Sessao.id == sessao_id)
        .where(Sessao.status == StatusSessao.AGENDADA)
        .where(Sessao.data_hora <= agora - DURACAO_SESSAO)
        .values(status=StatusSessao.REALIZADA)
        .execution_options(synchronize_session=False)
    )
    if resultado.rowcount != 1:
        db.rollback()
        raise JaRegistrada
    db.add(Avaliacao(sessao_id=sessao_id, comparecimento=comparecimento, arquivo_url=arquivo, liberado_em=agora_utc))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise JaRegistrada from None


def registrar_comparecimento(
    db: Session, funcionario_id: int, sessao_id: int, arquivo: BinaryIO, nome_enviado: str | None,
    agora: datetime, agora_utc: datetime, url_base: str, tarefas: BackgroundTasks | None = None,
) -> None:
    sessao = buscar_sessao_do_profissional(db, funcionario_id, sessao_id, agora.date())
    validar_registravel(sessao.status, sessao.data_hora, agora)
    nome = salvar_arquivo(arquivo, nome_enviado)
    try:
        _concluir_sessao(db, sessao_id, agora, agora_utc, True, nome)
    except BaseException:
        db.rollback()
        apagar_arquivo(nome)
        raise
    if tarefas is not None:
        destinatario, corpo = montar_email_nova_avaliacao(db, sessao_id, url_base)
        tarefas.add_task(enviar_email, destinatario, ASSUNTO_NOVA_AVALIACAO, corpo)


def registrar_nao_comparecimento(
    db: Session, funcionario_id: int, sessao_id: int, agora: datetime, agora_utc: datetime,
) -> None:
    sessao = buscar_sessao_do_profissional(db, funcionario_id, sessao_id, agora.date())
    validar_registravel(sessao.status, sessao.data_hora, agora)
    try:
        _concluir_sessao(db, sessao_id, agora, agora_utc, False, None)
    except BaseException:
        db.rollback()
        raise


def montar_email_nova_avaliacao(db: Session, sessao_id: int, url_base: str) -> tuple[str, str]:
    data_hora, paciente_id, atendido, nome, email = db.execute(
        select(Sessao.data_hora, Paciente.id, Paciente.nome, Usuario.nome, Usuario.email)
        .join(Plano, Plano.id == Sessao.plano_id)
        .join(Paciente, Paciente.id == Plano.paciente_id)
        .join(Usuario, Usuario.id == Paciente.cliente_id)
        .where(Sessao.id == sessao_id)
    ).one()
    corpo = templates.get_template("email/nova_avaliacao.html").render(
        nome=nome,
        atendido=atendido,
        data_sessao=data_hora.date(),
        link=f"{url_base.rstrip('/')}/historico?atendido={paciente_id}",
    )
    return email, corpo


# --- Download (RN03 e RNF08) ---------------------------------------------------------------------

def pode_acessar_avaliacao(db: Session, usuario: Usuario, paciente_id: int, cliente_id: int) -> bool:
    if usuario.tipo == TipoUsuario.ADMIN:
        return True
    if usuario.tipo == TipoUsuario.CLIENTE:
        return usuario.id == cliente_id
    if usuario.tipo == TipoUsuario.FUNCIONARIO:
        # "Consulta" = sessão confirmada; plano só aguardando pagamento não cria vínculo com o atendido
        return bool(db.scalar(select(exists().where(
            Plano.paciente_id == paciente_id,
            Plano.funcionario_id == usuario.id,
            Sessao.plano_id == Plano.id,
            Sessao.status.in_((StatusSessao.AGENDADA, StatusSessao.REALIZADA)),
        ))))
    return False


def buscar_arquivo(db: Session, usuario: Usuario, avaliacao_id: int) -> ArquivoAvaliacao:
    linha = db.execute(
        select(Avaliacao.arquivo_url, Sessao.data_hora, Paciente.id, Paciente.cliente_id)
        .join(Sessao, Sessao.id == Avaliacao.sessao_id)
        .join(Plano, Plano.id == Sessao.plano_id)
        .join(Paciente, Paciente.id == Plano.paciente_id)
        .where(Avaliacao.id == avaliacao_id)
    ).first()
    if linha is None:
        raise AvaliacaoNaoEncontrada
    nome, data_hora, paciente_id, cliente_id = linha
    if not nome or not pode_acessar_avaliacao(db, usuario, paciente_id, cliente_id):
        raise AvaliacaoNaoEncontrada
    return ArquivoAvaliacao(avaliacao_id, nome, data_hora.date())
