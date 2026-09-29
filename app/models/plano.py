import enum
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, Enum, ForeignKey, Integer, Numeric, String, Text, Time
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class StatusPlano(str, enum.Enum):
    ATIVO = "ativo"
    ENCERRADO = "encerrado"


class Plano(Base):
    __tablename__ = "plano"

    id: Mapped[int] = mapped_column(primary_key=True)
    paciente_id: Mapped[int] = mapped_column(ForeignKey("paciente.id"), nullable=False)
    funcionario_id: Mapped[int] = mapped_column(ForeignKey("funcionario.usuario_id"), nullable=False)
    especialidade_id: Mapped[int] = mapped_column(ForeignKey("especialidade.id"), nullable=False)

    dia_semana: Mapped[str] = mapped_column(String(50), nullable=False)
    horario: Mapped[datetime] = mapped_column(Time, nullable=False)
    frequencia_semanal: Mapped[int] = mapped_column(Integer, nullable=False)
    duracao: Mapped[str] = mapped_column(String(20), nullable=False)
    valor_total: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    data_inicio: Mapped[date] = mapped_column(Date, nullable=False)
    data_fim: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[StatusPlano] = mapped_column(
        # values_callable obrigatório: sem isso, o SQLAlchemy salva o NOME do enum Python (maiúsculo), não o valor — quebra contra o enum do Postgres
        Enum(StatusPlano, name="status_plano", values_callable=lambda enum_cls: [e.value for e in enum_cls]), nullable=False, default=StatusPlano.ATIVO
    )
    observacao: Mapped[str | None] = mapped_column(Text)
    criado_em: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    def __repr__(self) -> str:
        return f"<Plano id={self.id} status={self.status}>"
