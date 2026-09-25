from datetime import date

from sqlalchemy import Date, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Paciente(Base):
    __tablename__ = "paciente"

    id: Mapped[int] = mapped_column(primary_key=True)
    nome: Mapped[str] = mapped_column(String(255), nullable=False)
    data_nascimento: Mapped[date] = mapped_column(Date, nullable=False)
    cliente_id: Mapped[int] = mapped_column(ForeignKey("cliente.usuario_id"), nullable=False)

    def __repr__(self) -> str:
        return f"<Paciente id={self.id} nome={self.nome!r}>"
