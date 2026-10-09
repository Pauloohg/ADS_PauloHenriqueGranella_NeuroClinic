from datetime import datetime

import pytest
from sqlalchemy import event

from app.core.config import HORARIOS_ATENDIMENTO
from app.models.plano import Plano
from app.models.sessao import Sessao, StatusSessao
from app.services.plano_service import (
    DuracaoInvalida,
    comparar_conflitos,
    disponibilidade_semanal,
    ocupa_horario,
    verificar_conflitos,
)
from tests.fabricas import criar_especialidade, criar_funcionario, criar_plano_com_sessao

SEG, TER, QUA, QUI, SEX = range(5)
SESSAO = datetime(2026, 8, 10, 14, 0)  # segunda, horário local
PROPOSTAS = [SESSAO]
AGORA = datetime(2026, 8, 3, 10, 0)  # local


@pytest.fixture
def fono(db):
    return criar_especialidade(db, "Fonoaudiologia", "150.00")


@pytest.fixture
def carla(db, fono):
    return criar_funcionario(db, fono, email="carla@exemplo.com", nome="Carla Fono")


def _ocupar(db, funcionario, especialidade, data_hora, status=StatusSessao.AGENDADA, criado_em=datetime(2026, 8, 1, 12, 0)):
    plano = db.query(Plano).filter_by(funcionario_id=funcionario.usuario.id).first()
    if plano is None:
        criar_plano_com_sessao(db, funcionario.usuario.id, especialidade, status, data_hora=data_hora, criado_em=criado_em)
    else:
        db.add(Sessao(plano_id=plano.id, data_hora=data_hora, status=status))
        db.commit()


def _contar_consultas(db, funcao):
    consultas = []
    motor = db.get_bind()
    contar = lambda *args, **kwargs: consultas.append(args[2])  # noqa: E731
    event.listen(motor, "before_cursor_execute", contar)
    try:
        resultado = funcao()
    finally:
        event.remove(motor, "before_cursor_execute", contar)
    return resultado, consultas


# --- RN09: prazo de pagamento de 4 horas ----------------------------------------------------------

def test_pendente_dentro_do_prazo_bloqueia(db, fono, carla):
    # Plano criado às 12:00 UTC = 09:00 local; prazo até 13:00 local; agora 10:00 local
    _ocupar(db, carla, fono, SESSAO, StatusSessao.PENDENTE_PAGAMENTO, criado_em=datetime(2026, 8, 3, 12, 0))
    assert verificar_conflitos(db, carla.usuario.id, PROPOSTAS, AGORA) == [SESSAO]


def test_pendente_vencida_nao_bloqueia(db, fono, carla):
    # Criado às 08:00 UTC = 05:00 local; prazo até 09:00 local; agora 10:00 local
    _ocupar(db, carla, fono, SESSAO, StatusSessao.PENDENTE_PAGAMENTO, criado_em=datetime(2026, 8, 3, 8, 0))
    assert verificar_conflitos(db, carla.usuario.id, PROPOSTAS, AGORA) == []


def test_agendada_nunca_vence(db, fono, carla):
    _ocupar(db, carla, fono, SESSAO, StatusSessao.AGENDADA, criado_em=datetime(2026, 1, 1, 12, 0))
    assert verificar_conflitos(db, carla.usuario.id, PROPOSTAS, AGORA) == [SESSAO]


@pytest.mark.parametrize("agora, bloqueia", [(AGORA, True), (datetime(2026, 8, 3, 10, 1), False)])
def test_limite_exato_do_prazo(db, fono, carla, agora, bloqueia):
    # Criado às 09:00 UTC = 06:00 local; criado_em + 4h = 10:00 local: ainda bloqueia às 10:00, libera às 10:01
    _ocupar(db, carla, fono, SESSAO, StatusSessao.PENDENTE_PAGAMENTO, criado_em=datetime(2026, 8, 3, 9, 0))
    assert verificar_conflitos(db, carla.usuario.id, PROPOSTAS, agora) == ([SESSAO] if bloqueia else [])


@pytest.mark.parametrize("agora, bloqueia", [(datetime(2026, 8, 4, 1, 59), True), (datetime(2026, 8, 4, 2, 30), False)])
def test_prazo_converte_criado_em_de_utc_para_local(db, fono, carla, agora, bloqueia):
    # 01:00 UTC de 04/08 = 22:00 local de 03/08; o prazo termina às 02:00 local de 04/08.
    # Sem a conversão, 01:00 + 4h = 05:00 e a pendente das 02:30 ainda bloquearia.
    _ocupar(db, carla, fono, SESSAO, StatusSessao.PENDENTE_PAGAMENTO, criado_em=datetime(2026, 8, 4, 1, 0))
    assert verificar_conflitos(db, carla.usuario.id, PROPOSTAS, agora) == ([SESSAO] if bloqueia else [])


def test_ocupa_horario_regra_pura():
    criado = datetime(2026, 8, 3, 9, 0)  # UTC = 06:00 local
    assert ocupa_horario(StatusSessao.PENDENTE_PAGAMENTO, criado, datetime(2026, 8, 3, 10, 0)) is True
    assert ocupa_horario(StatusSessao.PENDENTE_PAGAMENTO, criado, datetime(2026, 8, 3, 10, 0, 1)) is False
    assert ocupa_horario(StatusSessao.AGENDADA, criado, datetime(2030, 1, 1)) is True


def test_verificar_conflitos_com_prazo_continua_com_uma_consulta(db, fono, carla):
    _ocupar(db, carla, fono, SESSAO, StatusSessao.PENDENTE_PAGAMENTO, criado_em=datetime(2026, 8, 3, 12, 0))
    funcionario_id = carla.usuario.id
    conflitos, consultas = _contar_consultas(db, lambda: verificar_conflitos(db, funcionario_id, PROPOSTAS, AGORA))
    assert conflitos == [SESSAO] and len(consultas) == 1


# --- comparar_conflitos (pura) ---------------------------------------------------------------------

def test_comparar_conflitos_pura():
    ocupados = [datetime(2026, 8, 3, 10, 30), datetime(2026, 8, 5, 14, 0)]
    propostas = [datetime(2026, 8, 3, h) for h in (9, 10, 11, 14)] + [datetime(2026, 8, 5, 15), datetime(2026, 8, 5, 14)]
    assert comparar_conflitos(propostas, ocupados) == [
        datetime(2026, 8, 3, 10), datetime(2026, 8, 3, 11), datetime(2026, 8, 5, 14),
    ]
    assert comparar_conflitos(propostas, []) == []


# --- disponibilidade_semanal ------------------------------------------------------------------------

def _todas_livres():
    return {dia: list(HORARIOS_ATENDIMENTO) for dia in range(5)}


def test_sem_sessoes_todas_as_celulas_livres(db, carla):
    assert disponibilidade_semanal(db, carla.usuario.id, "mensal", datetime(2026, 8, 1, 10, 0)) == _todas_livres()


def test_celula_ocupada_por_sessao_futura_sai_da_lista(db, fono, carla):
    # agora = sáb 01/08; plano mensal das quartas 14h: 05, 12, 19, 26/08 e 02/09; a sessão de 12/08 ocupa a célula
    _ocupar(db, carla, fono, datetime(2026, 8, 12, 14, 0))
    livres = disponibilidade_semanal(db, carla.usuario.id, "mensal", datetime(2026, 8, 1, 10, 0))
    esperado = _todas_livres()
    esperado[QUA].remove(14)
    assert livres == esperado


def test_sessao_fora_da_janela_nao_ocupa_a_celula(db, fono, carla):
    # Quinzenal das quartas a partir de 05/08: 05 e 12/08; uma sessão em 19/08 fica fora da janela
    _ocupar(db, carla, fono, datetime(2026, 8, 19, 14, 0))
    assert disponibilidade_semanal(db, carla.usuario.id, "quinzenal", datetime(2026, 8, 1, 10, 0)) == _todas_livres()
    assert 14 not in disponibilidade_semanal(db, carla.usuario.id, "mensal", datetime(2026, 8, 1, 10, 0))[QUA]


def test_sessao_em_feriado_pulado_nao_ocupa_a_celula(db, fono, carla):
    # agora = sex 28/08; mensal das segundas: 31/08, [07/09 feriado], 14, 21, 28/09 e 05/10
    _ocupar(db, carla, fono, datetime(2026, 9, 7, 14, 0))
    livres = disponibilidade_semanal(db, carla.usuario.id, "mensal", datetime(2026, 8, 28, 10, 0))
    assert 14 in livres[SEG]


def test_sessao_na_extensao_por_feriado_ocupa_a_celula(db, fono, carla):
    # Mesmo plano: 05/10 está fora da janela nominal [31/08, 30/09), mas entra no plano por causa do feriado
    _ocupar(db, carla, fono, datetime(2026, 10, 5, 14, 0))
    livres = disponibilidade_semanal(db, carla.usuario.id, "mensal", datetime(2026, 8, 28, 10, 0))
    assert 14 not in livres[SEG]
    assert livres[TER] == list(HORARIOS_ATENDIMENTO)


def test_pendente_vencida_nao_ocupa_a_celula(db, fono, carla):
    _ocupar(db, carla, fono, datetime(2026, 8, 12, 14, 0), StatusSessao.PENDENTE_PAGAMENTO, criado_em=datetime(2026, 7, 1, 12, 0))
    assert disponibilidade_semanal(db, carla.usuario.id, "mensal", datetime(2026, 8, 1, 10, 0)) == _todas_livres()


def test_sessao_de_outro_funcionario_nao_ocupa_a_celula(db, fono, carla):
    colega = criar_funcionario(db, fono, email="colega@exemplo.com")
    _ocupar(db, colega, fono, datetime(2026, 8, 12, 14, 0))
    assert disponibilidade_semanal(db, carla.usuario.id, "mensal", datetime(2026, 8, 1, 10, 0)) == _todas_livres()


def test_disponibilidade_faz_uma_unica_consulta(db, fono, carla):
    for dia in (3, 12, 21):
        _ocupar(db, carla, fono, datetime(2026, 8, dia, 9, 0))
    funcionario_id = carla.usuario.id
    livres, consultas = _contar_consultas(
        db, lambda: disponibilidade_semanal(db, funcionario_id, "anual", datetime(2026, 8, 1, 10, 0))
    )
    assert len(consultas) == 1
    assert 9 not in livres[SEG] and 9 not in livres[QUA] and 9 not in livres[SEX]
    assert 9 in livres[TER] and 9 in livres[QUI]


def test_disponibilidade_com_duracao_invalida(db, carla):
    with pytest.raises(DuracaoInvalida):
        disponibilidade_semanal(db, carla.usuario.id, "semestral", datetime(2026, 8, 1, 10, 0))


def test_grade_respeita_a_antecedencia_da_rn10(db, fono, carla):
    # agora = seg 03/08 10:01: a sessão das 14h de hoje está a 3h59, então o plano das segundas 14h começa em 10/08
    _ocupar(db, carla, fono, datetime(2026, 8, 3, 14, 0))
    assert 14 in disponibilidade_semanal(db, carla.usuario.id, "quinzenal", datetime(2026, 8, 3, 10, 1))[SEG]
    # Às 10:00 em ponto, 03/08 14h é elegível e a sessão existente ocupa a célula
    assert 14 not in disponibilidade_semanal(db, carla.usuario.id, "quinzenal", datetime(2026, 8, 3, 10, 0))[SEG]
