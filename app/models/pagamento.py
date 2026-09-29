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
        # values_callable obrigatório: sem isso, o SQLAlchemy salva o NOME do enum Python (maiúsculo), não o valor — quebra contra o enum do Postgres
        Enum(FormaPagamento, name="forma_pagamento_enum", values_callable=lambda enum_cls: [e.value for e in enum_cls])
    )
    status: Mapped[StatusPagamento] = mapped_column(
        # values_callable obrigatório: sem isso, o SQLAlchemy salva o NOME do enum Python (maiúsculo), não o valor — quebra contra o enum do Postgres
        Enum(StatusPagamento, name="status_pagamento", values_callable=lambda enum_cls: [e.value for e in enum_cls]), nullable=False, default=StatusPagamento.PENDENTE
    )
    confirmado_em: Mapped[datetime | None] = mapped_column(DateTime)

    def __repr__(self) -> str:
        return f"<Pagamento id={self.id} plano_id={self.plano_id} status={self.status}>"
