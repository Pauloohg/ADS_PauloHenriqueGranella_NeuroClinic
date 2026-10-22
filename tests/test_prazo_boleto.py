from datetime import date, datetime, time, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from app.models.cliente import Cliente
from app.models.pagamento import FormaPagamento, Pagamento, StatusPagamento
from app.models.plano import Plano
from app.models.sessao import Sessao, StatusSessao
from app.routers import cliente as cliente_router
from app.routers import inicio as inicio_router
from app.services import pagamento_service, plano_service
from tests.abacatepay_falsa import cobranca_boleto, cobranca_pix, expires_at_do_boleto
from tests.fabricas import (
    criar_cliente,
    criar_especialidade,
    criar_funcionario,
    criar_pagamento,
    criar_paciente,
    criar_plano,
    criar_plano_com_sessao,
    logar,
)

PIX = FormaPagamento.PIX
BOLETO = FormaPagamento.BOLETO
PENDENTE = StatusSessao.PENDENTE_PAGAMENTO
AGENDADA = StatusSessao.AGENDADA

AGORA = datetime(2026, 8, 3, 10, 0)  # segunda, horário local
AGORA_UTC = datetime(2026, 8, 3, 13, 0, tzinfo=timezone.utc)
CRIADO_EM_UTC = datetime(2026, 8, 3, 12, 0)  # 09:00 local: Pix até 13:00 local
SEGUNDAS = [datetime(2026, 8, dia, 14, 0) for dia in (17, 24, 31)]
VENCIMENTO = date(2026, 8, 6)  # quinta: 3 dias úteis depois da segunda 03/08
FIM_VENCIMENTO = datetime(2026, 8, 6, 23, 59, 59)
FIM_CARENCIA = datetime(2026, 8, 7, 23, 59, 59)  # + 1 dia útil


def _fixar_relogio(monkeypatch, agora_local, agora_utc=AGORA_UTC):
    for modulo in (cliente_router, inicio_router):
        monkeypatch.setattr(modulo, "agora_local", lambda: agora_local)
    monkeypatch.setattr(cliente_router, "agora_utc", lambda: agora_utc)


@pytest.fixture(autouse=True)
def relogio_fixo(monkeypatch):
    _fixar_relogio(monkeypatch, AGORA)


@pytest.fixture
def fono(db):
    return criar_especialidade(db, "Fonoaudiologia", "150.00")


@pytest.fixture
def carla(db, fono):
    return criar_funcionario(db, fono, email="carla@exemplo.com", nome="Carla Fono")


@pytest.fixture
def maria(db):
    usuario = criar_cliente(db, email="maria@exemplo.com", nome="Maria Souza")
    db.get(Cliente, usuario.id).cpf = "529.982.247-25"
    db.commit()
    return usuario


@pytest.fixture
def pedro(db, maria):
    return criar_paciente(db, maria.id, nome="Pedro Souza")


@pytest.fixture
def plano(db, carla, pedro):
    return criar_plano(db, pedro, carla, [(data, PENDENTE) for data in SEGUNDAS], criado_em=CRIADO_EM_UTC, valor_total="450.00")


@pytest.fixture
def plano_boleto(db, plano):
    _pagamento(db, plano, BOLETO)
    return plano


def _pagamento(db, plano, forma, status=StatusPagamento.PENDENTE, cobranca_id="bole_teste123"):
    pagamento = criar_pagamento(db, plano, status=status)
    pagamento.forma_pagamento, pagamento.abacatepay_payment_id = forma, cobranca_id
    db.commit()
    return pagamento


def _html(resposta) -> str:
    return " ".join(resposta.text.split())


def _pagar(client, plano, forma):
    return client.post(f"/planos/{plano.id}/pagamento", data={"forma": forma}, follow_redirects=False)


# --- Vencimento do boleto (função pura) --------------------------------------------------------

@pytest.mark.parametrize("criacao, primeira_sessao, esperado", [
    (date(2026, 8, 3), date(2026, 8, 31), date(2026, 8, 6)),  # segunda -> quinta
    (date(2026, 8, 7), date(2026, 8, 31), date(2026, 8, 12)),  # sexta -> quarta
    (date(2026, 8, 8), date(2026, 8, 31), date(2026, 8, 12)),  # sábado conta como dia 0: seg, ter, qua
    (date(2026, 9, 4), date(2026, 9, 28), date(2026, 9, 10)),  # sexta; segunda 07/09 é feriado e é pulada
    (date(2026, 8, 3), date(2026, 8, 6), date(2026, 8, 4)),  # primeira sessão quinta: 2º dia útil antes é terça
    (date(2026, 8, 3), date(2026, 8, 5), date(2026, 8, 3)),  # primeira sessão em 2 dias úteis: vence hoje
    (date(2026, 8, 5), date(2026, 8, 10), date(2026, 8, 6)),  # primeira sessão na segunda: vence na quinta
    (date(2026, 9, 2), date(2026, 9, 8), date(2026, 9, 3)),  # terça 08/09 depois do feriado: sexta e quinta antes
    (date(2026, 8, 3), date(2026, 8, 4), date(2026, 7, 31)),  # primeira sessão amanhã: venceria antes de hoje
])
def test_vencimento_do_boleto(criacao, primeira_sessao, esperado):
    assert plano_service.vencimento_boleto(criacao, primeira_sessao) == esperado


def test_boleto_so_e_oferecido_se_vencer_depois_de_hoje():
    vencimento = plano_service.vencimento_boleto(date(2026, 8, 3), date(2026, 8, 4))  # primeira sessão amanhã
    assert not plano_service.boleto_disponivel(vencimento, AGORA)
    assert not plano_service.boleto_disponivel(date(2026, 8, 2), AGORA)
    assert plano_service.boleto_disponivel(date(2026, 8, 4), AGORA)
    assert plano_service.boleto_disponivel(date(2026, 8, 4), datetime(2026, 8, 3, 23, 59, 59))


@pytest.mark.parametrize("dia, util", [
    (date(2026, 8, 3), True), (date(2026, 8, 8), False), (date(2026, 8, 9), False), (date(2026, 9, 7), False),
])
def test_dia_util_exclui_fim_de_semana_e_feriado_nacional(dia, util):
    assert plano_service.eh_dia_util(dia) is util


# --- Prazo de pagamento (função pura) ----------------------------------------------------------

def test_prazo_do_pix_e_sem_forma_continua_4_horas():
    esperado = datetime(2026, 8, 3, 13, 0)
    assert plano_service.prazo_pagamento(CRIADO_EM_UTC) == esperado
    assert plano_service.prazo_pagamento(CRIADO_EM_UTC, date(2026, 8, 17), None) == esperado
    assert plano_service.prazo_pagamento(CRIADO_EM_UTC, date(2026, 8, 17), PIX) == esperado


def test_prazo_do_boleto_e_o_fim_do_dia_do_vencimento():
    assert plano_service.prazo_pagamento(CRIADO_EM_UTC, date(2026, 8, 17), BOLETO) == FIM_VENCIMENTO


def test_prazo_do_boleto_usa_a_data_local_de_criacao():
    # 02:00 UTC de quinta 06/08 = 23:00 local de quarta 05/08: conta de quarta (vence segunda 10/08), não de quinta
    assert plano_service.prazo_pagamento(datetime(2026, 8, 6, 2, 0), date(2026, 8, 31), BOLETO) == datetime(
        2026, 8, 10, 23, 59, 59
    )


@pytest.mark.parametrize("criado_em_utc, primeira_sessao, esperado", [
    (CRIADO_EM_UTC, date(2026, 8, 17), FIM_CARENCIA),  # vence quinta, remove depois de sexta
    (datetime(2026, 8, 5, 12, 0), date(2026, 8, 31), datetime(2026, 8, 11, 23, 59, 59)),  # vence segunda 10/08
    (datetime(2026, 8, 6, 12, 0), date(2026, 8, 31), datetime(2026, 8, 12, 23, 59, 59)),  # vence terça 11/08
])
def test_limite_de_remocao_do_boleto_e_mais_1_dia_util(criado_em_utc, primeira_sessao, esperado):
    prazo = plano_service.prazo_pagamento(criado_em_utc, primeira_sessao, BOLETO)
    assert plano_service.limite_remocao(criado_em_utc, primeira_sessao, BOLETO) == datetime.combine(
        plano_service.somar_dias_uteis(prazo.date(), 1), time(23, 59, 59)
    ) == esperado


def test_limite_de_remocao_do_pix_continua_60_minutos():
    assert plano_service.limite_remocao(CRIADO_EM_UTC, date(2026, 8, 17), PIX) == datetime(2026, 8, 3, 14, 0)
    assert plano_service.limite_remocao(CRIADO_EM_UTC) == datetime(2026, 8, 3, 14, 0)


# --- Bloqueio do horário (RN01 + RN09) ---------------------------------------------------------

def test_sessoes_do_boleto_bloqueiam_depois_de_4h_e_param_no_vencimento(db, carla, plano_boleto):
    funcionario = carla.usuario.id
    for agora in (datetime(2026, 8, 3, 13, 0, 1), datetime(2026, 8, 5, 9, 0), FIM_VENCIMENTO):
        assert plano_service.verificar_conflitos(db, funcionario, [SEGUNDAS[0]], agora) == [SEGUNDAS[0]], agora
        assert plano_service.ocupa_horario(PENDENTE, plano_boleto.criado_em, agora, plano_boleto.data_inicio, BOLETO)

    depois = FIM_VENCIMENTO + timedelta(seconds=1)
    assert plano_service.verificar_conflitos(db, funcionario, [SEGUNDAS[0]], depois) == []
    assert 14 in plano_service.disponibilidade_semanal(db, funcionario, "mensal", depois)[0]


def test_sessoes_do_pix_continuam_liberando_aos_4h(db, carla, plano):
    _pagamento(db, plano, PIX, cobranca_id="pix_char_teste123")
    funcionario = carla.usuario.id
    assert plano_service.verificar_conflitos(db, funcionario, [SEGUNDAS[0]], datetime(2026, 8, 3, 13, 0)) == [SEGUNDAS[0]]
    assert plano_service.verificar_conflitos(db, funcionario, [SEGUNDAS[0]], datetime(2026, 8, 3, 13, 0, 1)) == []


def test_grade_da_contratacao_respeita_o_vencimento_do_boleto(client, db, monkeypatch, carla, plano_boleto, maria):
    logar(client, maria.email)
    _fixar_relogio(monkeypatch, datetime(2026, 8, 4, 10, 0))  # 4h já passaram; boleto ainda vale
    livres = client.get(f"/contratar/{carla.usuario.id}/disponibilidade?duracao=mensal").json()["livres"]
    assert 14 not in livres["0"]

    _fixar_relogio(monkeypatch, datetime(2026, 8, 7, 8, 0))  # boleto vencido
    livres = client.get(f"/contratar/{carla.usuario.id}/disponibilidade?duracao=mensal").json()["livres"]
    assert 14 in livres["0"]


# --- Limpeza dos planos vencidos ---------------------------------------------------------------

def _planos(db) -> set[int]:
    return set(db.scalars(select(Plano.id)))


def test_limpeza_do_boleto_so_depois_do_vencimento_mais_1_dia_util(db, plano_boleto):
    for agora in (datetime(2026, 8, 3, 14, 0, 1), FIM_VENCIMENTO + timedelta(seconds=1), FIM_CARENCIA):
        assert plano_service.remover_planos_vencidos(db, agora) == 0, agora
        assert _planos(db) == {plano_boleto.id}

    assert plano_service.remover_planos_vencidos(db, FIM_CARENCIA + timedelta(seconds=1)) == 1
    assert _planos(db) == set()
    assert db.query(Pagamento).count() == 0 and db.query(Sessao).count() == 0


def test_carencia_do_boleto_vencido_na_sexta_vai_ate_segunda(db, carla, pedro):
    # Criado na terça 04/08: vence na sexta 07/08 e a carência termina na segunda 10/08
    plano = criar_plano(db, pedro, carla, [(SEGUNDAS[0], PENDENTE)], criado_em=datetime(2026, 8, 4, 12, 0))
    _pagamento(db, plano, BOLETO)
    assert plano_service.remover_planos_vencidos(db, datetime(2026, 8, 9, 12, 0)) == 0
    assert plano_service.remover_planos_vencidos(db, datetime(2026, 8, 10, 23, 59, 59)) == 0
    assert plano_service.remover_planos_vencidos(db, datetime(2026, 8, 11, 0, 0)) == 1


def test_limpeza_nunca_remove_boleto_confirmado(db, plano):
    _pagamento(db, plano, BOLETO, status=StatusPagamento.CONFIRMADO)
    assert plano_service.remover_planos_vencidos(db, datetime(2026, 9, 30, 12, 0)) == 0
    assert _planos(db) == {plano.id}


def test_limpeza_do_pix_continua_com_60_minutos(db, carla, pedro, plano_boleto):
    pix = criar_plano(db, pedro, carla, [(datetime(2026, 8, 18, 9, 0), PENDENTE)], criado_em=CRIADO_EM_UTC)
    _pagamento(db, pix, PIX, cobranca_id="pix_char_teste123")
    criar_plano(db, pedro, carla, [(datetime(2026, 8, 19, 9, 0), PENDENTE)], criado_em=CRIADO_EM_UTC)  # sem forma
    boleto_id = plano_boleto.id

    assert plano_service.remover_planos_vencidos(db, datetime(2026, 8, 3, 14, 0)) == 0
    assert len(_planos(db)) == 3
    assert plano_service.remover_planos_vencidos(db, datetime(2026, 8, 3, 14, 0, 1)) == 2
    assert _planos(db) == {boleto_id}


# --- Escolha da forma e criação da cobrança ----------------------------------------------------

def test_escolha_mostra_pix_em_4_horas_e_vencimento_do_boleto(client, plano, maria, api_falsa):
    logar(client, maria.email)
    html = _html(client.get(f"/planos/{plano.id}/pagamento"))
    assert "Pague em até 4 horas" in html
    assert 'value="boleto"' in html and "Vence em 06/08/2026. O horário fica reservado até o vencimento." in html
    assert "boleto não está disponível" not in html
    assert "Vence hoje" not in html and "3 dias úteis" not in html
    assert api_falsa.requisicoes == []


def test_escolha_sem_boleto_quando_a_primeira_sessao_e_amanha(client, db, monkeypatch, carla, pedro, maria, api_falsa):
    # Criado às 09:00 de segunda; primeira sessão terça 04/08 às 14:00 (RN10 cumprida): o boleto venceria hoje
    plano = criar_plano(db, pedro, carla, [(datetime(2026, 8, 4, 14, 0), PENDENTE)], criado_em=CRIADO_EM_UTC)
    logar(client, maria.email)

    html = _html(client.get(f"/planos/{plano.id}/pagamento"))
    assert 'value="pix"' in html and 'value="boleto"' not in html
    assert "O boleto não está disponível para este plano porque a primeira sessão é muito próxima." in html


def test_post_forcado_de_boleto_indisponivel_e_recusado(client, db, carla, pedro, maria, api_falsa):
    plano = criar_plano(db, pedro, carla, [(datetime(2026, 8, 4, 14, 0), PENDENTE)], criado_em=CRIADO_EM_UTC)
    api_falsa.responder("/transparents/create", cobranca_boleto())
    logar(client, maria.email)

    resposta = _pagar(client, plano, "boleto")

    assert resposta.status_code == 400
    html = _html(resposta)
    assert "O boleto não está disponível para este plano porque a primeira sessão é muito próxima." in html
    assert 'value="pix"' in html and "Traceback" not in html and "dueDate" not in html
    assert db.query(Pagamento).count() == 0 and api_falsa.requisicoes == []


def test_pix_continua_disponivel_quando_o_boleto_nao_e(client, db, carla, pedro, maria, api_falsa):
    plano = criar_plano(db, pedro, carla, [(datetime(2026, 8, 4, 14, 0), PENDENTE)], criado_em=CRIADO_EM_UTC)
    api_falsa.responder("/transparents/create", cobranca_pix())
    logar(client, maria.email)

    assert _pagar(client, plano, "pix").status_code == 303
    [corpo] = api_falsa.corpos("/transparents/create")
    assert corpo["data"]["expiresIn"] == 3 * 3600 - 60 and "dueDate" not in corpo["data"]


def test_boleto_envia_o_due_date_do_vencimento(client, db, plano, maria, api_falsa):
    api_falsa.responder("/transparents/create", cobranca_boleto())
    logar(client, maria.email)

    assert _pagar(client, plano, "boleto").status_code == 303

    [corpo] = api_falsa.corpos("/transparents/create")
    assert corpo["method"] == "BOLETO" and corpo["data"]["dueDate"] == "2026-08-06"
    assert "expiresIn" not in corpo["data"]
    assert db.query(Pagamento).one().forma_pagamento == BOLETO


def test_escolha_do_boleto_so_dentro_das_4_horas(client, db, monkeypatch, plano, maria, api_falsa):
    _fixar_relogio(monkeypatch, datetime(2026, 8, 3, 13, 1))
    api_falsa.responder("/transparents/create", cobranca_boleto())
    logar(client, maria.email)

    assert client.get(f"/planos/{plano.id}/pagamento").status_code == 409
    resposta = _pagar(client, plano, "boleto")
    assert resposta.status_code == 409 and "terminou em 03/08/2026 às 13:00" in _html(resposta)
    assert api_falsa.requisicoes == []


def test_api_falsa_devolve_expires_at_no_fim_do_dia_do_due_date(api_falsa):
    api_falsa.responder("/transparents/create", cobranca_boleto())
    corpo = {"method": "BOLETO", "data": {"amount": 100, "description": "x", "customer": {"name": "x"}, "dueDate": "2026-08-06"}}

    resposta = api_falsa(httpx.Request("POST", "https://api.abacatepay.com/v2/transparents/create", json=corpo))
    assert resposta.json()["data"]["expiresAt"] == "2026-08-07T02:59:59.999Z" == expires_at_do_boleto("2026-08-06")

    corpo["data"]["dueDate"] = "06/08/2026"
    assert api_falsa(httpx.Request("POST", "https://api.abacatepay.com/v2/transparents/create", json=corpo)).status_code == 400


# --- Telas com o prazo do boleto ---------------------------------------------------------------

def test_pagina_do_boleto_mostra_vencimento(client, db, monkeypatch, plano, maria, api_falsa):
    api_falsa.responder("/transparents/create", cobranca_boleto())
    logar(client, maria.email)
    _pagar(client, plano, "boleto")

    _fixar_relogio(monkeypatch, datetime(2026, 8, 5, 9, 0))  # depois das 4h, antes do vencimento
    html = _html(client.get(f"/planos/{plano.id}/cobranca"))
    assert "Vence em 06/08/2026. O horário fica reservado até o vencimento." in html
    assert "Prazo limite: <strong>06/08/2026 às 23:59</strong>" in html
    assert "Aguardando pagamento" in html and "Prazo de pagamento encerrado" not in html
    assert "Vence hoje" not in html and "3 dias úteis" not in html


def test_pagina_do_boleto_vencido_mostra_prazo_encerrado(client, db, monkeypatch, plano_boleto, maria, api_falsa):
    _fixar_relogio(monkeypatch, datetime(2026, 8, 7, 0, 0))
    logar(client, maria.email)
    html = _html(client.get(f"/planos/{plano_boleto.id}/cobranca"))
    assert "Prazo de pagamento encerrado" in html and "06/08/2026 às 23:59" in html
    # Depois do prazo, só a conferência (rede de segurança do webhook) consulta a API, e nada é confirmado
    assert [r.url.path for r in api_falsa.requisicoes] == ["/v2/transparents/list"]
    assert db.query(Pagamento).one().status == StatusPagamento.PENDENTE


def test_selos_pague_ate_usam_o_vencimento_do_boleto(client, db, monkeypatch, carla, pedro, plano_boleto, maria):
    pix = criar_plano(db, pedro, carla, [(datetime(2026, 8, 18, 9, 0), PENDENTE)], criado_em=datetime(2026, 8, 4, 12, 0))
    _fixar_relogio(monkeypatch, datetime(2026, 8, 4, 10, 0))  # boleto além das 4h; Pix criado às 09:00
    logar(client, maria.email)

    inicio = _html(client.get("/"))
    assert "Pague até 06/08/2026 (vencimento do boleto)." in inicio
    assert "Pague até 04/08/2026 às 13:00 · restam 3h." in inicio
    assert inicio.count(">Aguardando pagamento</span>") == 2

    pagina = _html(client.get(f"/planos/{plano_boleto.id}"))
    assert "Pague até <strong>06/08/2026 às 23:59</strong>" in pagina
    assert _html(client.get(f"/planos/{pix.id}")).count("Pague até <strong>04/08/2026 às 13:00</strong>") == 1


def test_boleto_vencido_nao_aparece_como_proxima_sessao(client, db, monkeypatch, carla, pedro, maria):
    # Primeira sessão quinta 06/08: o boleto vence terça 04/08 e a carência termina quarta 05/08 (antes da sessão)
    plano = criar_plano(db, pedro, carla, [(datetime(2026, 8, 6, 14, 0), PENDENTE)], criado_em=CRIADO_EM_UTC)
    plano_id = plano.id
    _pagamento(db, plano, BOLETO)
    logar(client, maria.email)

    _fixar_relogio(monkeypatch, datetime(2026, 8, 5, 15, 0))  # vencido, ainda na carência
    inicio = _html(client.get("/"))
    assert "06/08" not in inicio and "Aguardando pagamento" not in inicio and "Pagar agora" not in inicio
    assert "O pagamento não foi feito até 04/08/2026 às 23:59" in _html(client.get(f"/planos/{plano_id}"))

    _fixar_relogio(monkeypatch, datetime(2026, 8, 6, 15, 0))  # a data da sessão já passou
    inicio = _html(client.get("/"))
    assert "06/08" not in inicio and "Aguardando pagamento" not in inicio
    assert _planos(db) == set()  # a folga de 2 dias úteis faz a carência terminar antes da sessão


# --- Webhook fora do prazo ---------------------------------------------------------------------

def _evento(cobranca_id="bole_teste123"):
    return {"event": "transparent.completed", "data": {"transparent": {"id": cobranca_id, "amount": 45000}}}


def test_webhook_de_boleto_depois_das_4h_mas_antes_do_vencimento_confirma(db, fono, carla, plano_boleto):
    # Um conflito só é revalidado fora do prazo; dentro do vencimento o horário era do boleto
    criar_plano_com_sessao(
        db, carla.usuario.id, fono, AGENDADA, data_hora=SEGUNDAS[0], criado_em=datetime(2026, 8, 4, 12, 0),
        email_cliente="joana@exemplo.com",
    )
    resultado = pagamento_service.processar_evento(db, _evento(), FIM_VENCIMENTO, datetime(2026, 8, 7, 2, 59))
    assert resultado.situacao == "confirmado"


def test_webhook_de_boleto_depois_do_vencimento_revalida_a_agenda(db, fono, carla, plano_boleto):
    depois = FIM_VENCIMENTO + timedelta(seconds=1)
    criar_plano_com_sessao(
        db, carla.usuario.id, fono, AGENDADA, data_hora=SEGUNDAS[0] + timedelta(minutes=30),
        criado_em=datetime(2026, 8, 7, 3, 0), email_cliente="joana@exemplo.com",
    )
    resultado = pagamento_service.processar_evento(db, _evento(), depois, datetime(2026, 8, 7, 3, 0, 1))
    assert resultado.situacao == "conflito"
    db.expire_all()
    assert db.scalar(select(Pagamento.status)) == StatusPagamento.PENDENTE


def test_webhook_de_boleto_depois_do_vencimento_sem_conflito_confirma(db, plano_boleto):
    depois = FIM_VENCIMENTO + timedelta(hours=10)
    resultado = pagamento_service.processar_evento(db, _evento(), depois, datetime(2026, 8, 7, 13, 0))
    assert resultado.situacao == "confirmado"
    assert set(db.scalars(select(Sessao.status))) == {AGENDADA}


# --- Primeira sessão real (RN06) ---------------------------------------------------------------

AGORA_SETEMBRO = datetime(2026, 9, 3, 10, 0)  # quinta; a segunda seguinte, 07/09, é feriado nacional
FORM_SEGUNDA = {"celula": ["0-14"], "frequencia_semanal": "1", "duracao": "quinzenal", "observacao": ""}


def _primeira_sessao(db, plano_id) -> date:
    return min(db.scalars(select(Sessao.data_hora).where(Sessao.plano_id == plano_id))).date()


def _confere_primeira_sessao_real(client, db, plano):
    # Com data_inicio = 07/09 (nominal), o 2º dia útil antes seria 03/09 (hoje) e o boleto não seria oferecido
    assert plano.data_inicio == _primeira_sessao(db, plano.id) == date(2026, 9, 14)
    html = _html(client.get(f"/planos/{plano.id}/pagamento"))
    assert 'value="boleto"' in html and "Vence em 09/09/2026." in html


def test_primeira_data_nominal_em_feriado_usa_a_primeira_sessao_gerada(client, db, monkeypatch, carla, pedro, maria):
    _fixar_relogio(monkeypatch, AGORA_SETEMBRO, datetime(2026, 9, 3, 13, 0, tzinfo=timezone.utc))
    logar(client, maria.email)

    resposta = client.post(
        f"/contratar/{carla.usuario.id}/confirmar", data={**FORM_SEGUNDA, "paciente_id": str(pedro.id)},
        follow_redirects=False,
    )

    assert resposta.status_code == 303
    _confere_primeira_sessao_real(client, db, db.scalars(select(Plano)).one())


def test_renovacao_com_primeira_data_nominal_em_feriado(client, db, monkeypatch, carla, pedro, maria):
    import re

    encerrado = criar_plano(
        db, pedro, carla, [(datetime(2026, 8, d, 14, 0), StatusSessao.REALIZADA) for d in (17, 24, 31)],
        criado_em=datetime(2026, 8, 3, 12, 0),
    )
    _fixar_relogio(monkeypatch, AGORA_SETEMBRO, datetime(2026, 9, 3, 13, 0, tzinfo=timezone.utc))
    logar(client, maria.email)

    html = client.get(f"/planos/{encerrado.id}/renovar").text
    form = re.search(r'<form method="post" action="([^"]+)" id="form-renovar-direto">(.*?)</form>', html, re.S)
    campos = re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', form.group(2))
    dados: dict[str, list[str]] = {}
    for nome, valor in campos:
        dados.setdefault(nome, []).append(valor)
    assert client.post(form.group(1), data=dados, follow_redirects=False).status_code == 303

    novo = db.scalars(select(Plano).where(Plano.id != encerrado.id)).one()
    _confere_primeira_sessao_real(client, db, novo)
