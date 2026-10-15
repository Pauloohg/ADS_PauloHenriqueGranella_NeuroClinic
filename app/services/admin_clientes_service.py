from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from app.core.fuso import utc_para_local
from app.models.cliente import Cliente
from app.models.paciente import Paciente
from app.models.plano import Plano, StatusPlano
from app.models.sessao import Sessao, StatusSessao
from app.models.usuario import TipoUsuario, Usuario
from app.services.inicio_service import ResumoPlano, planos_do_cliente

POR_PAGINA = 20


class ClienteNaoEncontrado(Exception):
    pass


@dataclass(frozen=True)
class ClienteListado:
    id: int
    nome: str
    email: str
    telefone: str | None
    cadastrado_em: datetime
    pacientes: int
    planos_ativos: int


@dataclass(frozen=True)
class PaginaClientes:
    clientes: list[ClienteListado]
    pagina: int
    total_paginas: int
    total: int
    busca: str


@dataclass
class PacienteDetalhe:
    nome: str
    idade: int
    planos: list[ResumoPlano] = field(default_factory=list)


@dataclass(frozen=True)
class ClienteDetalhe:
    id: int
    nome: str
    email: str
    telefone: str | None
    cpf_mascarado: str | None
    cadastrado_em: datetime
    pacientes: list[PacienteDetalhe]


def condicao_plano_ativo():
    # Mesma "situação Ativo" do início do Cliente: aguardando pagamento (ou vencido, RN09) não conta
    pendente = exists().where(Sessao.plano_id == Plano.id, Sessao.status == StatusSessao.PENDENTE_PAGAMENTO)
    return (Plano.status == StatusPlano.ATIVO) & ~pendente


def mascarar_cpf(cpf: str | None) -> str | None:
    digitos = "".join(c for c in cpf or "" if c.isdigit())
    return f"***.***.***-{digitos[-2:]}" if len(digitos) == 11 else None


def calcular_idade(nascimento: date, hoje: date) -> int:
    return hoje.year - nascimento.year - ((hoje.month, hoje.day) < (nascimento.month, nascimento.day))


def interpretar_pagina(texto: str | None) -> int:
    return int(texto) if texto and texto.isdigit() and int(texto) > 0 else 1


def listar(db: Session, busca: str = "", pagina: int = 1) -> PaginaClientes:
    busca = busca.strip()
    pacientes = (
        select(Paciente.cliente_id, func.count().label("total")).group_by(Paciente.cliente_id).subquery()
    )
    ativos = (
        select(Paciente.cliente_id, func.count(Plano.id).label("total"))
        .join(Plano, Plano.paciente_id == Paciente.id)
        .where(condicao_plano_ativo())
        .group_by(Paciente.cliente_id)
        .subquery()
    )
    filtro = Usuario.tipo == TipoUsuario.CLIENTE
    if busca:
        filtro &= Usuario.nome.icontains(busca, autoescape=True) | Usuario.email.icontains(busca, autoescape=True)

    total = db.scalar(select(func.count()).select_from(Usuario).where(filtro))
    total_paginas = max(1, -(-total // POR_PAGINA))
    pagina = min(max(pagina, 1), total_paginas)

    consulta = (
        select(
            Usuario.id, Usuario.nome, Usuario.email, Cliente.telefone, Usuario.criado_em,
            func.coalesce(pacientes.c.total, 0), func.coalesce(ativos.c.total, 0),
        )
        .join(Cliente, Cliente.usuario_id == Usuario.id)
        .outerjoin(pacientes, pacientes.c.cliente_id == Usuario.id)
        .outerjoin(ativos, ativos.c.cliente_id == Usuario.id)
        .where(filtro)
        .order_by(func.lower(Usuario.nome), Usuario.id)
        .limit(POR_PAGINA)
        .offset((pagina - 1) * POR_PAGINA)
    )
    clientes = [
        ClienteListado(id_, nome, email, telefone, utc_para_local(criado_em), n_pacientes, n_ativos)
        for id_, nome, email, telefone, criado_em, n_pacientes, n_ativos in db.execute(consulta)
    ]
    return PaginaClientes(clientes, pagina, total_paginas, total, busca)


def detalhar(db: Session, cliente_id: int, agora: datetime) -> ClienteDetalhe:
    linha = db.execute(
        select(Usuario, Cliente)
        .join(Cliente, Cliente.usuario_id == Usuario.id)
        .where(Usuario.id == cliente_id, Usuario.tipo == TipoUsuario.CLIENTE)
    ).first()
    if linha is None:
        raise ClienteNaoEncontrado
    usuario, cliente = linha

    lista_pacientes = db.scalars(
        select(Paciente).where(Paciente.cliente_id == cliente_id).order_by(Paciente.nome, Paciente.id)
    ).all()
    planos_por_paciente: dict[int, list[ResumoPlano]] = defaultdict(list)
    for resumo in planos_do_cliente(db, cliente_id, agora):
        planos_por_paciente[resumo.plano.paciente_id].append(resumo)

    pacientes = [
        PacienteDetalhe(p.nome, calcular_idade(p.data_nascimento, agora.date()), planos_por_paciente[p.id])
        for p in lista_pacientes
    ]
    return ClienteDetalhe(
        usuario.id, usuario.nome, usuario.email, cliente.telefone, mascarar_cpf(cliente.cpf),
        utc_para_local(usuario.criado_em), pacientes,
    )
