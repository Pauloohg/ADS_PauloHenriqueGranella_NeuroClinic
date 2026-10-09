import enum
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import JSON, Date, DateTime, Enum, ForeignKey, Integer, Numeric, SmallInteger, String, Text, Time
from sqlalchemy.dialects.postgresql import ARRAY
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

    # 0 = segunda ... 6 = domingo (datetime.weekday()); o variant JSON é só para a suíte de testes em SQLite
    dias_semana: Mapped[list[int]] = mapped_column(ARRAY(SmallInteger).with_variant(JSON(), "sqlite"), nullable=False)
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
