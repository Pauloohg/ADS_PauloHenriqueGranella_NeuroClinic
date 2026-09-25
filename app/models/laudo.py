from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Laudo(Base):
    __tablename__ = "laudo"

    id: Mapped[int] = mapped_column(primary_key=True)
    sessao_id: Mapped[int] = mapped_column(ForeignKey("sessao.id"), nullable=False, unique=True)
    arquivo_url: Mapped[str | None] = mapped_column(String(255))
    comparecimento: Mapped[bool] = mapped_column(Boolean, nullable=False)
    liberado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    def __repr__(self) -> str:
        return f"<Laudo id={self.id} sessao_id={self.sessao_id} comparecimento={self.comparecimento}>"
