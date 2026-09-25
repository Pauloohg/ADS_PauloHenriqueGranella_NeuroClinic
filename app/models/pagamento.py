import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class FormaPagamento(str, enum.Enum):
    PIX = "pix"
    BOLETO = "boleto"


class StatusPagamento(str, enum.Enum):
    PENDENTE = "pendente"
    CONFIRMADO = "confirmado"


class Pagamento(Base):
    __tablename__ = "pagamento"

    id: Mapped[int] = mapped_column(primary_key=True)
    plano_id: Mapped[int] = mapped_column(ForeignKey("plano.id"), nullable=False, unique=True)
    abacatepay_payment_id: Mapped[str | None] = mapped_column(String(50), unique=True)
    forma_pagamento: Mapped[FormaPagamento | None] = mapped_column(
        Enum(FormaPagamento, name="forma_pagamento_enum")
    )
    status: Mapped[StatusPagamento] = mapped_column(
        Enum(StatusPagamento, name="status_pagamento"), nullable=False, default=StatusPagamento.PENDENTE
    )
    confirmado_em: Mapped[datetime | None] = mapped_column(DateTime)

    def __repr__(self) -> str:
        return f"<Pagamento id={self.id} plano_id={self.plano_id} status={self.status}>"
