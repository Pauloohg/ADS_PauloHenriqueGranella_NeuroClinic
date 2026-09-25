from datetime import datetime

from sqlalchemy import DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class HistoricoRemarcacao(Base):
    __tablename__ = "historico_remarcacao"

    id: Mapped[int] = mapped_column(primary_key=True)
    sessao_id: Mapped[int] = mapped_column(ForeignKey("sessao.id"), nullable=False)
    data_hora_original: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    data_hora_nova: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    solicitado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    def __repr__(self) -> str:
        return f"<HistoricoRemarcacao id={self.id} sessao_id={self.sessao_id}>"
