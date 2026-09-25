import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class StatusSessao(str, enum.Enum):
    PENDENTE_PAGAMENTO = "pendente_pagamento"
    AGENDADA = "agendada"
    REALIZADA = "realizada"


class Sessao(Base):
    __tablename__ = "sessao"

    id: Mapped[int] = mapped_column(primary_key=True)
    plano_id: Mapped[int] = mapped_column(ForeignKey("plano.id"), nullable=False)
    data_hora: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    status: Mapped[StatusSessao] = mapped_column(
        Enum(StatusSessao, name="status_sessao"), nullable=False, default=StatusSessao.PENDENTE_PAGAMENTO
    )
    foi_remarcada: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    def __repr__(self) -> str:
        return f"<Sessao id={self.id} status={self.status}>"
