from sqlalchemy import Boolean, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Funcionario(Base):
    __tablename__ = "funcionario"

    usuario_id: Mapped[int] = mapped_column(ForeignKey("usuario.id"), primary_key=True)
    especialidade_id: Mapped[int] = mapped_column(ForeignKey("especialidade.id"), nullable=False)
    ativo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Contato público mostrado ao Cliente no RF04; email_contato é separado do e-mail de login e nunca é copiado dele
    telefone_contato: Mapped[str | None] = mapped_column(String(20))
    email_contato: Mapped[str | None] = mapped_column(String(120))
    formacao: Mapped[str | None] = mapped_column(String(300))
    registro_profissional: Mapped[str | None] = mapped_column(String(50))
    apresentacao: Mapped[str | None] = mapped_column(String(500))

    def __repr__(self) -> str:
        return f"<Funcionario usuario_id={self.usuario_id} ativo={self.ativo}>"
