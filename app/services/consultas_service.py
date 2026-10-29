from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.especialidade import Especialidade
from app.models.paciente import Paciente
from app.models.plano import Plano
from app.models.sessao import Sessao, StatusSessao
from app.models.usuario import Usuario
from app.services.admin_clientes_service import POR_PAGINA
from app.services.plano_service import DURACAO_SESSAO
from app.services.remarcacao_service import motivo_bloqueio

PROXIMAS = "proximas"
ANTERIORES = "anteriores"
ABAS = (PROXIMAS, ANTERIORES)


@dataclass(frozen=True)
class ConsultaCliente:
    id: int
    data_hora: datetime
    fim: datetime
    atendido: str
    profissional: str
    especialidade: str
    status: StatusSessao
    remarcada: bool
    motivo_bloqueio: str | None

    @property
    def pode_remarcar(self) -> bool:
        return self.motivo_bloqueio is None


@dataclass(frozen=True)
class PaginaConsultas:
    consultas: list[ConsultaCliente]
    aba: str
    pagina: int
    total_paginas: int
    total: int


def _consulta(cliente_id: int, aba: str, agora: datetime, *colunas):
    filtro = Sessao.data_hora >= agora if aba == PROXIMAS else Sessao.data_hora < agora
    return (
        select(*colunas)
        .select_from(Sessao)
        .join(Plano, Plano.id == Sessao.plano_id)
        .join(Paciente, Paciente.id == Plano.paciente_id)
        .where(Paciente.cliente_id == cliente_id, filtro)
    )


def consultas_do_cliente(db: Session, cliente_id: int, agora: datetime, aba: str = PROXIMAS, pagina: int = 1) -> PaginaConsultas:
    aba = aba if aba in ABAS else PROXIMAS
    pagina = max(pagina, 1)
    ordem = (Sessao.data_hora, Sessao.id) if aba == PROXIMAS else (Sessao.data_hora.desc(), Sessao.id.desc())
    colunas = (
        Sessao.id, Sessao.data_hora, Paciente.nome, Usuario.nome, Especialidade.nome, Sessao.status,
        Sessao.foi_remarcada, func.count().over(),
    )

    def buscar(numero: int):
        return db.execute(
            _consulta(cliente_id, aba, agora, *colunas)
            .join(Especialidade, Especialidade.id == Plano.especialidade_id)
            .join(Usuario, Usuario.id == Plano.funcionario_id)
            .order_by(*ordem)
            .limit(POR_PAGINA)
            .offset((numero - 1) * POR_PAGINA)
        ).all()

    linhas = buscar(pagina)
    if not linhas and pagina > 1:
        total = db.scalar(_consulta(cliente_id, aba, agora, func.count(Sessao.id)))
        pagina = max(1, -(-total // POR_PAGINA))
        linhas = buscar(pagina) if total else []

    total = linhas[0][-1] if linhas else 0
    consultas = [
        ConsultaCliente(
            id_, data_hora, data_hora + DURACAO_SESSAO, atendido, profissional, especialidade, status, remarcada,
            motivo_bloqueio(status, data_hora, agora),
        )
        for id_, data_hora, atendido, profissional, especialidade, status, remarcada, _ in linhas
    ]
    return PaginaConsultas(consultas, aba, pagina, max(1, -(-total // POR_PAGINA)), total)
