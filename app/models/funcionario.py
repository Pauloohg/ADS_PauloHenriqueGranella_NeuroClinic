from sqlalchemy import Boolean, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Funcionario(Base):
    __tablename__ = "funcionario"

    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuario.id"), primary_key=True)
    especialidade_id: Mapped[int] = mapped_column(ForeignKey("especialidade.id"), nullable=False)
    ativo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    def __repr__(self) -> str:
        return f"<Funcionario usuario_id={self.usuario_id} ativo={self.ativo}>"
