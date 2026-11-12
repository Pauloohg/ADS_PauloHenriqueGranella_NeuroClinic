from collections import defaultdict
from dataclasses import dataclass, field, fields
from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.fuso import utc_para_local
from app.core.ids import inteiro_positivo, somente_digitos
from app.core.templating import descrever_configuracao
from app.models.cliente import Cliente
from app.models.pagamento import FormaPagamento
from app.models.paciente import Paciente
from app.models.plano import Plano
from app.models.usuario import TipoUsuario, Usuario
from app.services.contratacao_service import prazo_previsto
from app.services.inicio_service import AGUARDANDO_PAGAMENTO, ATIVO, ENCERRADO, ResumoPlano, planos_do_cliente
from app.services.plano_service import prazo_pagamento
from app.services.plano_service import condicao_plano_ativo

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


@dataclass(frozen=True)
class PlanoDoCliente(ResumoPlano):
    configuracao: str = ""
    prazo_texto: str | None = None


@dataclass
class PacienteDetalhe:
    nome: str
    idade: int
    planos: list[PlanoDoCliente] = field(default_factory=list)

    @property
    def planos_em_aberto(self) -> list[PlanoDoCliente]:
        return [p for p in self.planos if p.situacao != ENCERRADO]

    @property
    def planos_encerrados(self) -> list[PlanoDoCliente]:
        return [p for p in self.planos if p.situacao == ENCERRADO]


@dataclass(frozen=True)
class ClienteDetalhe:
    id: int
    nome: str
    email: str
    telefone: str | None
    cpf_mascarado: str | None
    cadastrado_em: datetime
    pacientes: list[PacienteDetalhe]

    @property
    def planos_ativos(self) -> int:
        return sum(1 for p in self.pacientes for plano in p.planos if plano.situacao == ATIVO)

    @property
    def resumo(self) -> str:
        return texto_resumo_do_cliente(len(self.pacientes), self.planos_ativos)


def mascarar_cpf(cpf: str | None) -> str | None:
    digitos = somente_digitos(cpf)
    return f"***.***.***-{digitos[-2:]}" if len(digitos) == 11 else None


def calcular_idade(nascimento: date, hoje: date) -> int:
    return hoje.year - nascimento.year - ((hoje.month, hoje.day) < (nascimento.month, nascimento.day))


def interpretar_pagina(texto: str | None) -> int:
    return inteiro_positivo(texto) or 1


def _plural(quantidade: int, singular: str, plural: str) -> str:
    return f"{quantidade} {singular if quantidade == 1 else plural}"


def texto_contagem(total: int, pagina: int, total_paginas: int, busca: str) -> str:
    if total_paginas > 1:
        inicio = (pagina - 1) * POR_PAGINA + 1
        return f"Clientes {inicio} a {min(pagina * POR_PAGINA, total)} de {total}"
    if busca:
        return _plural(total, "cliente encontrado", "clientes encontrados")
    return _plural(total, "cliente", "clientes")


def texto_resumo_do_cliente(atendidos: int, planos_ativos: int) -> str:
    if not atendidos:
        return "Nenhum atendido cadastrado."
    ativos = _plural(planos_ativos, "plano ativo", "planos ativos") if planos_ativos else "nenhum plano ativo"
    return f"{_plural(atendidos, 'atendido', 'atendidos')} e {ativos}."


def texto_prazo(
    criado_em_utc: datetime, data_primeira_sessao: date, forma: FormaPagamento | None, agora: datetime
) -> str:
    if forma == FormaPagamento.BOLETO:
        return prazo_pagamento(criado_em_utc, data_primeira_sessao, forma).strftime("%d/%m/%Y")
    if forma is None:  # cobrança ainda não escolhida: o prazo é o do Pix, como no resumo da contratação
        return prazo_previsto(criado_em_utc, data_primeira_sessao, agora).pix.strftime("%d/%m/%Y, %H:%M")
    return prazo_pagamento(criado_em_utc, data_primeira_sessao, forma).strftime("%d/%m/%Y, %H:%M")


def _plano_do_cliente(resumo: ResumoPlano, agora: datetime) -> PlanoDoCliente:
    plano = resumo.plano
    prazo = None
    if resumo.situacao == AGUARDANDO_PAGAMENTO:
        prazo = texto_prazo(plano.criado_em, plano.data_inicio, resumo.forma, agora)
    return PlanoDoCliente(
        **{campo.name: getattr(resumo, campo.name) for campo in fields(ResumoPlano)},
        configuracao=descrever_configuracao(plano.duracao, plano.frequencia_semanal, plano.dias_semana, plano.horario),
        prazo_texto=prazo,
    )


def listar(db: Session, agora: datetime, busca: str = "", pagina: int = 1) -> PaginaClientes:
    busca = busca.strip()
    pacientes = (
        select(Paciente.cliente_id, func.count().label("total")).group_by(Paciente.cliente_id).subquery()
    )
    ativos = (
        select(Paciente.cliente_id, func.count(Plano.id).label("total"))
        .join(Plano, Plano.paciente_id == Paciente.id)
        .where(condicao_plano_ativo(agora))
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
    planos_por_paciente: dict[int, list[PlanoDoCliente]] = defaultdict(list)
    for resumo in planos_do_cliente(db, cliente_id, agora):
        planos_por_paciente[resumo.plano.paciente_id].append(_plano_do_cliente(resumo, agora))

    pacientes = [
        PacienteDetalhe(p.nome, calcular_idade(p.data_nascimento, agora.date()), planos_por_paciente[p.id])
        for p in lista_pacientes
    ]
    return ClienteDetalhe(
        usuario.id, usuario.nome, usuario.email, cliente.telefone, mascarar_cpf(cliente.cpf),
        utc_para_local(usuario.criado_em), pacientes,
    )
