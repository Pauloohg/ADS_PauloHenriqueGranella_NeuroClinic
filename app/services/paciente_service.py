from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.paciente import Paciente
from app.schemas.paciente import PacienteDados

DATA_NASCIMENTO_MINIMA = date(1900, 1, 1)


class PacienteNaoEncontrado(Exception):
    pass


class DataNascimentoInvalida(Exception):
    pass


def _validar_data_nascimento(data_nascimento: date, hoje: date) -> None:
    if data_nascimento > hoje:
        raise DataNascimentoInvalida("A data de nascimento não pode estar no futuro.")
    if data_nascimento < DATA_NASCIMENTO_MINIMA:
        raise DataNascimentoInvalida("Confira o ano da data de nascimento.")


def listar_do_cliente(db: Session, cliente_id: int) -> list[Paciente]:
    consulta = select(Paciente).where(Paciente.cliente_id == cliente_id).order_by(Paciente.nome)
    return list(db.scalars(consulta))


# Paciente de outro Cliente responde igual a inexistente, para não revelar que o id existe
def buscar_do_cliente(db: Session, paciente_id: int, cliente_id: int) -> Paciente:
    paciente = db.get(Paciente, paciente_id)
    if paciente is None or paciente.cliente_id != cliente_id:
        raise PacienteNaoEncontrado
    return paciente


def cadastrar(db: Session, cliente_id: int, dados: PacienteDados, hoje: date) -> Paciente:
    _validar_data_nascimento(dados.data_nascimento, hoje)
    paciente = Paciente(nome=dados.nome, data_nascimento=dados.data_nascimento, cliente_id=cliente_id)
    db.add(paciente)
    db.commit()
    db.refresh(paciente)
    return paciente


def atualizar(db: Session, paciente_id: int, cliente_id: int, dados: PacienteDados, hoje: date) -> Paciente:
    paciente = buscar_do_cliente(db, paciente_id, cliente_id)
    _validar_data_nascimento(dados.data_nascimento, hoje)
    paciente.nome = dados.nome
    paciente.data_nascimento = dados.data_nascimento
    db.commit()
    return paciente
