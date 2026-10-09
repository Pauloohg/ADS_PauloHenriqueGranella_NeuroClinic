from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.core import security
from app.models.especialidade import Especialidade
from app.models.funcionario import Funcionario
from app.models.plano import Plano
from app.models.sessao import Sessao, StatusSessao
from app.models.usuario import TipoUsuario, Usuario
from app.schemas.funcionario import FuncionarioCadastro, FuncionarioEdicao
from app.services import especialidade_service
from app.services.auth_service import EmailJaCadastrado, buscar_por_email, utc_sem_fuso


class FuncionarioNaoEncontrado(Exception):
    pass


class FuncionarioComSessoesFuturas(Exception):
    pass


class EspecialidadeComSessoesFuturas(Exception):
    pass


STATUS_QUE_PRENDEM = (StatusSessao.AGENDADA, StatusSessao.PENDENTE_PAGAMENTO)


@dataclass
class FuncionarioResumo:
    usuario: Usuario
    funcionario: Funcionario
    especialidade: Especialidade


def listar(db: Session) -> list[FuncionarioResumo]:
    consulta = (
        select(Usuario, Funcionario, Especialidade)
        .join(Funcionario, Funcionario.usuario_id == Usuario.id)
        .join(Especialidade, Especialidade.id == Funcionario.especialidade_id)
        .order_by(Funcionario.ativo.desc(), Usuario.nome)
    )
    return [FuncionarioResumo(*linha) for linha in db.execute(consulta)]


def listar_ativos_da_especialidade(db: Session, especialidade_id: int) -> list[Usuario]:
    consulta = (
        select(Usuario)
        .join(Funcionario, Funcionario.usuario_id == Usuario.id)
        .where(Funcionario.especialidade_id == especialidade_id, Funcionario.ativo.is_(True))
        .order_by(Usuario.nome)
    )
    return list(db.scalars(consulta))


def buscar(db: Session, funcionario_id: int) -> FuncionarioResumo:
    funcionario = db.get(Funcionario, funcionario_id)
    if funcionario is None:
        raise FuncionarioNaoEncontrado
    return FuncionarioResumo(
        db.get(Usuario, funcionario_id), funcionario, db.get(Especialidade, funcionario.especialidade_id)
    )


def cadastrar(db: Session, dados: FuncionarioCadastro, agora: datetime) -> FuncionarioResumo:
    especialidade_service.buscar(db, dados.especialidade_id)
    if buscar_por_email(db, dados.email) is not None:
        raise EmailJaCadastrado

    usuario = Usuario(
        nome=dados.nome,
        email=dados.email,
        senha_hash=security.gerar_hash_senha(dados.senha),
        tipo=TipoUsuario.FUNCIONARIO,
        criado_em=utc_sem_fuso(agora),
    )
    db.add(usuario)
    db.flush()  # não remover: Funcionario precisa de usuario.id antes do commit
    db.add(Funcionario(usuario_id=usuario.id, especialidade_id=dados.especialidade_id, ativo=True))
    db.commit()
    return buscar(db, usuario.id)


def atualizar(db: Session, funcionario_id: int, dados: FuncionarioEdicao, agora: datetime) -> FuncionarioResumo:
    resumo = buscar(db, funcionario_id)
    especialidade_service.buscar(db, dados.especialidade_id)
    trocou_especialidade = dados.especialidade_id != resumo.funcionario.especialidade_id
    if trocou_especialidade and possui_sessoes_futuras(db, funcionario_id, agora, STATUS_QUE_PRENDEM):
        raise EspecialidadeComSessoesFuturas
    resumo.usuario.nome = dados.nome
    resumo.funcionario.especialidade_id = dados.especialidade_id
    db.commit()
    return buscar(db, funcionario_id)


def possui_sessoes_futuras(
    db: Session, funcionario_id: int, agora: datetime, status: tuple[StatusSessao, ...]
) -> bool:
    return db.scalar(
        select(
            exists()
            .where(Sessao.plano_id == Plano.id)
            .where(Plano.funcionario_id == funcionario_id)
            .where(Sessao.status.in_(status))
            .where(Sessao.data_hora >= agora)
        )
    )


def desativar(db: Session, funcionario_id: int, agora: datetime) -> FuncionarioResumo:
    resumo = buscar(db, funcionario_id)
    if possui_sessoes_futuras(db, funcionario_id, agora, STATUS_QUE_PRENDEM):
        raise FuncionarioComSessoesFuturas  # RN05
    resumo.funcionario.ativo = False
    db.commit()
    return resumo


def reativar(db: Session, funcionario_id: int) -> FuncionarioResumo:
    resumo = buscar(db, funcionario_id)
    resumo.funcionario.ativo = True
    db.commit()
    return resumo
