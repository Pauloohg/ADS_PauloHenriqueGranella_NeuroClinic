from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from app.core import security
from app.models.especialidade import Especialidade
from app.models.funcionario import Funcionario
from app.models.plano import Plano
from app.models.sessao import Sessao, StatusSessao
from app.models.usuario import TipoUsuario, Usuario
from app.schemas.funcionario import CAMPOS_CONTATO, ContatoProfissional, FuncionarioCadastro, FuncionarioEdicao
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


# Sem o e-mail de login de propósito: é o único objeto do Profissional que chega às telas do Cliente
@dataclass(frozen=True)
class PerfilProfissional:
    id: int
    nome: str
    formacao: str | None
    registro_profissional: str | None
    apresentacao: str | None
    telefone_contato: str | None
    email_contato: str | None

    @property
    def tem_contato(self) -> bool:
        return bool(self.telefone_contato or self.email_contato)


def perfil_publico(usuario: Usuario, funcionario: Funcionario) -> PerfilProfissional:
    return PerfilProfissional(
        usuario.id, usuario.nome, funcionario.formacao, funcionario.registro_profissional,
        funcionario.apresentacao, funcionario.telefone_contato, funcionario.email_contato,
    )


def perfis_ativos_da_especialidade(db: Session, especialidade_id: int) -> list[PerfilProfissional]:
    consulta = (
        select(Usuario, Funcionario)
        .join(Funcionario, Funcionario.usuario_id == Usuario.id)
        .where(Funcionario.especialidade_id == especialidade_id, Funcionario.ativo.is_(True))
        .order_by(Usuario.nome)
    )
    return [perfil_publico(usuario, funcionario) for usuario, funcionario in db.execute(consulta)]


def _aplicar_contato(funcionario: Funcionario, dados: ContatoProfissional) -> None:
    for campo in CAMPOS_CONTATO:
        setattr(funcionario, campo, getattr(dados, campo))


def listar_ativos_da_especialidade(db: Session, especialidade_id: int) -> list[Usuario]:
    consulta = (
        select(Usuario)
        .join(Funcionario, Funcionario.usuario_id == Usuario.id)
        .where(Funcionario.especialidade_id == especialidade_id, Funcionario.ativo.is_(True))
        .order_by(Usuario.nome)
    )
    return list(db.scalars(consulta))


def especialidades_com_profissional_ativo(db: Session) -> set[int]:
    consulta = select(Funcionario.especialidade_id).where(Funcionario.ativo.is_(True)).distinct()
    return set(db.scalars(consulta))


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
    funcionario = Funcionario(usuario_id=usuario.id, especialidade_id=dados.especialidade_id, ativo=True)
    _aplicar_contato(funcionario, dados)
    db.add(funcionario)
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
    _aplicar_contato(resumo.funcionario, dados)
    db.commit()
    return buscar(db, funcionario_id)


def condicoes_sessao_futura(agora: datetime, status: tuple[StatusSessao, ...] = STATUS_QUE_PRENDEM):
    # RN05 e RN07: a desativação, a troca de especialidade e as contagens das telas usam SÓ este filtro
    return (Sessao.status.in_(status), Sessao.data_hora >= agora)


def possui_sessoes_futuras(
    db: Session, funcionario_id: int, agora: datetime, status: tuple[StatusSessao, ...]
) -> bool:
    return db.scalar(
        select(
            exists()
            .where(Sessao.plano_id == Plano.id)
            .where(Plano.funcionario_id == funcionario_id)
            .where(*condicoes_sessao_futura(agora, status))
        )
    )


def _futuras_por_profissional(agora: datetime):
    return (
        select(Plano.funcionario_id, func.count(Sessao.id).label("futuras"))
        .join(Sessao, Sessao.plano_id == Plano.id)
        .where(*condicoes_sessao_futura(agora))
        .group_by(Plano.funcionario_id)
        .subquery()
    )


def contar_sessoes_futuras(db: Session, funcionario_id: int, agora: datetime) -> int:
    return db.scalar(
        select(func.count(Sessao.id))
        .join(Plano, Plano.id == Sessao.plano_id)
        .where(Plano.funcionario_id == funcionario_id, *condicoes_sessao_futura(agora))
    )


def _ativos_por_especialidade():
    # Contagem única de ativos: lista de Profissionais, lista de Especialidades e confirmação de desativação
    return (
        select(Funcionario.especialidade_id, func.count(Funcionario.usuario_id).label("ativos"))
        .where(Funcionario.ativo.is_(True))
        .group_by(Funcionario.especialidade_id)
        .subquery()
    )


def contar_ativos_da_especialidade(db: Session, especialidade_id: int) -> int:
    ativos = _ativos_por_especialidade()
    return db.scalar(select(ativos.c.ativos).where(ativos.c.especialidade_id == especialidade_id)) or 0


@dataclass(frozen=True)
class EspecialidadeNaLista:
    id: int
    nome: str
    valor_sessao: Decimal
    ativos: int


def listar_especialidades_com_ativos(db: Session) -> list[EspecialidadeNaLista]:
    ativos = _ativos_por_especialidade()
    linhas = db.execute(
        select(Especialidade.id, Especialidade.nome, Especialidade.valor_sessao, func.coalesce(ativos.c.ativos, 0))
        .outerjoin(ativos, ativos.c.especialidade_id == Especialidade.id)
        .order_by(Especialidade.nome, Especialidade.id)
    ).all()
    return [EspecialidadeNaLista(i, nome, valor, int(n)) for i, nome, valor, n in linhas]


@dataclass(frozen=True)
class ProfissionalNaLista:
    id: int
    nome: str
    email: str
    ativo: bool
    consultas_futuras: int


@dataclass(frozen=True)
class GrupoDeEspecialidade:
    id: int
    nome: str
    ativos: int
    profissionais: list[ProfissionalNaLista]

    @property
    def desativados(self) -> int:
        return len(self.profissionais) - self.ativos


def listar_por_especialidade(db: Session, agora: datetime) -> list[GrupoDeEspecialidade]:
    futuras = _futuras_por_profissional(agora)
    ativos = _ativos_por_especialidade()
    linhas = db.execute(
        select(
            Especialidade.id, Especialidade.nome, func.coalesce(ativos.c.ativos, 0), Usuario.id, Usuario.nome,
            Usuario.email, Funcionario.ativo, func.coalesce(futuras.c.futuras, 0),
        )
        .select_from(Especialidade)
        .outerjoin(ativos, ativos.c.especialidade_id == Especialidade.id)
        .outerjoin(Funcionario, Funcionario.especialidade_id == Especialidade.id)
        .outerjoin(Usuario, Usuario.id == Funcionario.usuario_id)
        .outerjoin(futuras, futuras.c.funcionario_id == Funcionario.usuario_id)
        .order_by(Especialidade.nome, Especialidade.id, Funcionario.ativo.desc(), Usuario.nome, Usuario.id)
    ).all()
    grupos: dict[int, GrupoDeEspecialidade] = {}
    for esp_id, esp_nome, qtd_ativos, usuario_id, nome, email, ativo, n in linhas:
        grupo = grupos.setdefault(esp_id, GrupoDeEspecialidade(esp_id, esp_nome, int(qtd_ativos), []))
        if usuario_id is not None:
            grupo.profissionais.append(ProfissionalNaLista(usuario_id, nome, email, bool(ativo), int(n)))
    return list(grupos.values())


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
