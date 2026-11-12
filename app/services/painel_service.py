import re
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

from dateutil.relativedelta import relativedelta
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.fuso import local_para_utc
from app.core.templating import NOMES_MESES, moeda_fixa, percentual
from app.models.especialidade import Especialidade
from app.models.funcionario import Funcionario
from app.models.pagamento import Pagamento, StatusPagamento
from app.models.paciente import Paciente
from app.models.plano import Plano
from app.models.usuario import TipoUsuario, Usuario
from app.services.plano_service import condicao_plano_ativo

TAMANHO_RANKING = 5
ZERO = Decimal("0.00")
CENTAVO = Decimal("0.01")
_FORMATO_MES = re.compile(r"^([0-9]{4})-([0-9]{2})$")


@dataclass(frozen=True)
class Variacao:
    valor: Decimal
    percentual: Decimal | None


@dataclass(frozen=True)
class LinhaRanking:
    nome: str
    especialidade: str
    ativo: bool
    planos: int
    receita: Decimal
    posicao: int = 0


@dataclass(frozen=True)
class Comparacao:
    tipo: str  # "andamento", "vazio", "sem_anterior" ou "fechado"
    texto: str
    ate: str | None = None
    largura_mes: int = 0
    largura_anterior: int = 0


@dataclass(frozen=True)
class LinhaEspecialidade:
    nome: str
    clientes_ativos: int
    planos: int
    receita: Decimal
    destaque: bool
    largura_clientes: int
    largura_planos: int
    largura_receita: int


@dataclass(frozen=True)
class ResumoDoMes:
    receita: Decimal
    planos_contratados: int
    novos_clientes: int


@dataclass(frozen=True)
class Painel:
    mes: date
    mes_anterior: date
    mes_seguinte: date | None
    mes_ajustado: bool
    receita: Decimal
    receita_anterior: Decimal
    variacao: Variacao
    comparacao: Comparacao
    planos_contratados: int
    novos_clientes: int
    total_clientes: int
    planos_ativos: int
    funcionarios_ativos: int
    ranking: list[LinhaRanking]
    especialidades: list[LinhaEspecialidade]

    @property
    def vazio(self) -> bool:
        return self.planos_contratados == 0


def resolver_mes(texto: str | None, agora: datetime) -> tuple[date, bool]:
    atual = agora.date().replace(day=1)
    encontrado = _FORMATO_MES.match(texto or "")
    if encontrado is None:
        return atual, bool(texto)
    ano, mes = int(encontrado[1]), int(encontrado[2])
    if not 1 <= mes <= 12 or ano < 2000:
        return atual, True
    escolhido = date(ano, mes, 1)
    if escolhido > atual:
        return atual, True
    return escolhido, False


def limites_utc(mes: date) -> tuple[datetime, datetime]:
    # O mês é fechado no horário de Brasília; confirmado_em e criado_em são gravados em UTC
    inicio = datetime(mes.year, mes.month, 1)
    return local_para_utc(inicio), local_para_utc(inicio + relativedelta(months=1))


def _dinheiro(valor) -> Decimal:
    return Decimal(str(valor or 0)).quantize(CENTAVO)


def calcular_variacao(atual: Decimal, anterior: Decimal) -> Variacao:
    diferenca = atual - anterior
    if anterior == 0:
        return Variacao(diferenca, None)
    return Variacao(diferenca, (diferenca * 100 / anterior).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def nome_do_mes(mes: date) -> str:
    return NOMES_MESES[mes.month - 1]


def comparar_com_anterior(
    mes: date, mes_corrente: date, agora: datetime, receita: Decimal, receita_anterior: Decimal
) -> Comparacao:
    anterior = nome_do_mes(mes - relativedelta(months=1))
    if mes == mes_corrente:
        # Mês incompleto contra mês inteiro: a porcentagem enganaria, então só o total do anterior
        return Comparacao("andamento", f"{anterior.capitalize()} inteiro: {moeda_fixa(receita_anterior)}.",
                          ate=agora.strftime("%d/%m"))
    if receita == 0:
        return Comparacao("vazio", "Nenhum pagamento confirmado neste mês.")
    maior = max(receita, receita_anterior)
    larguras = {"largura_mes": _largura(receita, maior), "largura_anterior": _largura(receita_anterior, maior)}
    if receita_anterior == 0:
        return Comparacao("sem_anterior", f"Nenhum pagamento confirmado em {anterior}.", **larguras)
    variacao = calcular_variacao(receita, receita_anterior).percentual
    if variacao == 0:
        texto = f"Igual a {anterior}."
    else:
        texto = f"{percentual(abs(variacao))} {'a mais' if variacao > 0 else 'a menos'} que {anterior}."
    return Comparacao("fechado", texto, **larguras)


def posicoes_densas(linhas: list[LinhaRanking]) -> list[LinhaRanking]:
    resultado, posicao, anterior = [], 0, None
    for linha in linhas:
        if linha.planos != anterior:
            posicao, anterior = posicao + 1, linha.planos
        resultado.append(replace(linha, posicao=posicao))
    return resultado


def _confirmados_no_mes(mes: date):
    inicio, fim = limites_utc(mes)
    return (
        Pagamento.status == StatusPagamento.CONFIRMADO,
        Pagamento.confirmado_em >= inicio,
        Pagamento.confirmado_em < fim,
    )


def _contratos(db: Session, mes: date) -> tuple[int, Decimal]:
    quantidade, receita = db.execute(
        select(func.count(Plano.id), func.sum(Plano.valor_total))
        .join(Pagamento, Pagamento.plano_id == Plano.id)
        .where(*_confirmados_no_mes(mes))
    ).one()
    return quantidade, _dinheiro(receita)


def _novos_clientes(db: Session, mes: date) -> int:
    inicio, fim = limites_utc(mes)
    return db.scalar(
        select(func.count()).select_from(Usuario)
        .where(Usuario.tipo == TipoUsuario.CLIENTE, Usuario.criado_em >= inicio, Usuario.criado_em < fim)
    )


def resumo_do_mes(db: Session, mes: date) -> ResumoDoMes:
    planos, receita = _contratos(db, mes)
    return ResumoDoMes(receita, planos, _novos_clientes(db, mes))


def _ranking(db: Session, mes: date) -> list[LinhaRanking]:
    quantidade = func.count(Plano.id)
    receita = func.sum(Plano.valor_total)
    consulta = (
        select(Usuario.nome, Especialidade.nome, Funcionario.ativo, quantidade, receita)
        .select_from(Plano)
        .join(Pagamento, Pagamento.plano_id == Plano.id)
        .join(Funcionario, Funcionario.usuario_id == Plano.funcionario_id)
        .join(Usuario, Usuario.id == Funcionario.usuario_id)
        .join(Especialidade, Especialidade.id == Funcionario.especialidade_id)
        .where(*_confirmados_no_mes(mes))
        .group_by(Funcionario.usuario_id, Usuario.nome, Especialidade.nome, Funcionario.ativo)
        .order_by(quantidade.desc(), receita.desc(), func.lower(Usuario.nome), Funcionario.usuario_id)
        .limit(TAMANHO_RANKING)
    )
    return posicoes_densas([
        LinhaRanking(nome, especialidade, ativo, planos, _dinheiro(receita))
        for nome, especialidade, ativo, planos, receita in db.execute(consulta)
    ])


def _largura(valor, maximo) -> int:
    return round(100 * valor / maximo) if maximo else 0


def _por_especialidade(db: Session, mes: date, agora: datetime) -> list[LinhaEspecialidade]:
    clientes = (
        select(Plano.especialidade_id, func.count(func.distinct(Paciente.cliente_id)).label("total"))
        .join(Paciente, Paciente.id == Plano.paciente_id)
        .where(condicao_plano_ativo(agora))
        .group_by(Plano.especialidade_id)
        .subquery()
    )
    contratos = (
        select(Plano.especialidade_id, func.count(Plano.id).label("planos"), func.sum(Plano.valor_total).label("receita"))
        .join(Pagamento, Pagamento.plano_id == Plano.id)
        .where(*_confirmados_no_mes(mes))
        .group_by(Plano.especialidade_id)
        .subquery()
    )
    consulta = (
        select(Especialidade.nome, func.coalesce(clientes.c.total, 0), func.coalesce(contratos.c.planos, 0), contratos.c.receita)
        .outerjoin(clientes, clientes.c.especialidade_id == Especialidade.id)
        .outerjoin(contratos, contratos.c.especialidade_id == Especialidade.id)
        .order_by(Especialidade.nome, Especialidade.id)
    )
    linhas = [(nome, int(n), int(planos), _dinheiro(receita)) for nome, n, planos, receita in db.execute(consulta)]
    max_clientes = max((l[1] for l in linhas), default=0)
    max_planos = max((l[2] for l in linhas), default=0)
    max_receita = max((l[3] for l in linhas), default=ZERO)
    return [
        LinhaEspecialidade(
            nome, n_clientes, planos, receita,
            destaque=n_clientes > 0 and n_clientes == max_clientes,
            largura_clientes=_largura(n_clientes, max_clientes),
            largura_planos=_largura(planos, max_planos),
            largura_receita=_largura(receita, max_receita),
        )
        for nome, n_clientes, planos, receita in linhas
    ]


def montar_painel(db: Session, mes: date, agora: datetime, mes_ajustado: bool = False) -> Painel:
    atual = agora.date().replace(day=1)
    anterior = mes - relativedelta(months=1)
    seguinte = mes + relativedelta(months=1)
    resumo = resumo_do_mes(db, mes)
    _, receita_anterior = _contratos(db, anterior)
    return Painel(
        mes=mes,
        mes_anterior=anterior,
        mes_seguinte=seguinte if seguinte <= atual else None,
        mes_ajustado=mes_ajustado,
        receita=resumo.receita,
        receita_anterior=receita_anterior,
        variacao=calcular_variacao(resumo.receita, receita_anterior),
        comparacao=comparar_com_anterior(mes, atual, agora, resumo.receita, receita_anterior),
        planos_contratados=resumo.planos_contratados,
        novos_clientes=resumo.novos_clientes,
        total_clientes=db.scalar(select(func.count()).select_from(Usuario).where(Usuario.tipo == TipoUsuario.CLIENTE)),
        planos_ativos=db.scalar(select(func.count()).select_from(Plano).where(condicao_plano_ativo(agora))),
        funcionarios_ativos=db.scalar(select(func.count()).select_from(Funcionario).where(Funcionario.ativo.is_(True))),
        ranking=_ranking(db, mes),
        especialidades=_por_especialidade(db, mes, agora),
    )
