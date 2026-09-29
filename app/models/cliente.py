from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Cliente(Base):
    __tablename__ = "cliente"

    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuario.id"), primary_key=True)
    telefone: Mapped[str | None] = mapped_column(String(20))
    cpf: Mapped[str | None] = mapped_column(String(14), nullable=True, unique=True)  # nullable de propósito: CPF só é exigido na contratação (RF05)
    abacatepay_customer_id: Mapped[str | None] = mapped_column(String(50), unique=True)

    def __repr__(self) -> str:
        return f"<Cliente usuario_id={self.usuario_id} cpf={self.cpf!r}>"
