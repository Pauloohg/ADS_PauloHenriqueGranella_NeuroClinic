from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.cliente import Cliente
from app.models.usuario import Usuario
from app.schemas.cliente import PerfilClienteAtualizacao, validar_cpf
from app.services.auth_service import buscar_por_email


class DadosEmUso(Exception):
    def __init__(self, campos: set[str]):
        super().__init__(campos)
        self.campos = campos


def obter_cliente(db: Session, usuario: Usuario) -> Cliente:
    return db.get(Cliente, usuario.id)


def campos_em_uso(db: Session, usuario_id: int, email: str, cpf: str | None) -> set[str]:
    campos = set()
    dono_email = buscar_por_email(db, email.strip())
    if dono_email is not None and dono_email.id != usuario_id:
        campos.add("email")
    try:
        cpf = validar_cpf(cpf or "")
    except ValueError:
        cpf = None
    if cpf is not None and cpf_em_uso(db, usuario_id, cpf):
        campos.add("cpf")
    return campos


def cpf_em_uso(db: Session, usuario_id: int, cpf_formatado: str) -> bool:
    dono_cpf = db.scalar(select(Cliente).where(Cliente.cpf == cpf_formatado))
    return dono_cpf is not None and dono_cpf.usuario_id != usuario_id


def atualizar_perfil(db: Session, usuario: Usuario, dados: PerfilClienteAtualizacao) -> Cliente:
    em_uso = campos_em_uso(db, usuario.id, dados.email, dados.cpf)
    if em_uso:
        raise DadosEmUso(em_uso)

    cliente = obter_cliente(db, usuario)
    usuario.nome = dados.nome
    usuario.email = dados.email
    cliente.telefone = dados.telefone
    cliente.cpf = dados.cpf
    db.commit()
    return cliente


def texto_dados_faltando(telefone: str | None, cpf: str | None) -> str | None:
    faltam = [nome for nome, valor in (("o telefone", telefone), ("o CPF", cpf)) if not (valor or "").strip()]
    if not faltam:
        return None
    return f"{'Falta' if len(faltam) == 1 else 'Faltam'} {' e '.join(faltam)} para pagar um plano"
