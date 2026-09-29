import enum
from datetime import datetime

from sqlalchemy import Enum, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class TipoUsuario(str, enum.Enum):
    CLIENTE = "cliente"
    FUNCIONARIO = "funcionario"
    ADMIN = "admin"


class Usuario(Base):
    __tablename__ = "usuario"

    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    senha_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    tipo: Mapped[TipoUsuario] = mapped_column(
        # values_callable obrigatório: sem isso, o SQLAlchemy salva o NOME do enum Python (maiúsculo), não o valor — quebra contra o enum do Postgres
        Enum(TipoUsuario, name="tipo_usuario", values_callable=lambda enum_cls: [e.value for e in enum_cls]), nullable=False
    )
    criado_em: Mapped[datetime] = mapped_column(default=datetime.utcnow)

    def __repr__(self) -> str:
        return f"<Usuario id={self.id} email={self.email!r} tipo={self.tipo}>"
