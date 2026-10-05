from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.especialidade import Especialidade
from app.schemas.especialidade import EspecialidadeCadastro, EspecialidadeValor


class EspecialidadeNaoEncontrada(Exception):
    pass


class EspecialidadeJaCadastrada(Exception):
    pass


def listar(db: Session) -> list[Especialidade]:
    return list(db.scalars(select(Especialidade).order_by(Especialidade.nome)))


def buscar(db: Session, especialidade_id: int) -> Especialidade:
    especialidade = db.get(Especialidade, especialidade_id)
    if especialidade is None:
        raise EspecialidadeNaoEncontrada
    return especialidade


def cadastrar(db: Session, dados: EspecialidadeCadastro) -> Especialidade:
    existente = db.scalar(select(Especialidade).where(func.lower(Especialidade.nome) == dados.nome.lower()))
    if existente is not None:
        raise EspecialidadeJaCadastrada
    especialidade = Especialidade(nome=dados.nome, valor_sessao=dados.valor_sessao)
    db.add(especialidade)
    db.commit()
    db.refresh(especialidade)
    return especialidade


def atualizar_valor(db: Session, especialidade_id: int, dados: EspecialidadeValor) -> Especialidade:
    especialidade = buscar(db, especialidade_id)
    especialidade.valor_sessao = dados.valor_sessao
    db.commit()
    return especialidade
