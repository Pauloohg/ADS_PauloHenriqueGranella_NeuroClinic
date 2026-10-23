import re
from datetime import date, datetime, time, timezone
from decimal import Decimal

import pytest

from app.models.cliente import Cliente
from app.models.plano import Plano, StatusPlano
from app.models.sessao import Sessao, StatusSessao
from app.routers import cliente as cliente_router
from app.services import contratacao_service, funcionario_service
from tests.fabricas import (
    criar_admin,
    criar_cliente,
    criar_especialidade,
    criar_funcionario,
    criar_paciente,
    criar_plano_com_sessao,
    logar,
)

AGORA = datetime(2026, 8, 1, 10, 0)  # sábado, horário local
AGORA_UTC = datetime(2026, 8, 1, 13, 0, tzinfo=timezone.utc)  # o mesmo instante em UTC
CPF_VALIDO = "529.982.247-25"
SEGUNDAS_DE_AGOSTO = [datetime(2026, 8, dia, 14, 0) for dia in (3, 10, 17, 24, 31)]


def _fixar_relogio(monkeypatch, agora_local, agora_utc=None):
    monkeypatch.setattr(cliente_router, "agora_local", lambda: agora_local)
    monkeypatch.setattr(cliente_router, "agora_utc", lambda: agora_utc or AGORA_UTC)


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
    db.get(Cliente, usuario.id).cpf = CPF_VALIDO
    db.commit()
    return usuario


@pytest.fixture
def pedro(db, maria):
    return criar_paciente(db, maria.id, nome="Pedro Souza")


@pytest.fixture
def maria_logada(client, maria):
    logar(client, maria.email)
    return maria


def _url(funcionario):
    return f"/contratar/{funcionario.usuario.id}/confirmar"


def _form(paciente, celulas=("0-14",), frequencia="1", duracao="mensal", **extras):
    return {"paciente_id": str(paciente.id), "celula": list(celulas), "frequencia_semanal": frequencia,
            "duracao": duracao, "observacao": "", **extras}


def _confirmar(client, funcionario, dados):
    return client.post(_url(funcionario), data=dados, follow_redirects=False)


def _ocupar_por_outro_cliente(db, funcionario, especialidade, data_hora, status=StatusSessao.AGENDADA):
    return criar_plano_com_sessao(
        db, funcionario.usuario.id, especialidade, status, data_hora=data_hora,
        criado_em=datetime(2026, 8, 1, 12, 0), email_cliente="joana@exemplo.com",
    )


def _html(resposta) -> str:
    return " ".join(resposta.text.split())


# --- Criação -------------------------------------------------------------------------------------

def test_confirmacao_cria_plano_e_sessoes_pendentes(client, db, carla, pedro, maria_logada):
    resposta = _confirmar(client, carla, _form(pedro, observacao="  Prefere tarde  "))

    plano = db.query(Plano).one()
    assert resposta.status_code == 303 and resposta.headers["location"] == f"/planos/{plano.id}"
    assert (plano.paciente_id, plano.funcionario_id, plano.especialidade_id) == (pedro.id, carla.usuario.id, carla.especialidade.id)
    assert plano.status == StatusPlano.ATIVO
    # RN11: 5 sessões x R$ 150 = R$ 750,00; mensal tem 5% (R$ 37,50) e o plano guarda o valor cobrado
    assert (plano.desconto_percentual, plano.valor_desconto, plano.valor_total) == (5, Decimal("37.50"), Decimal("712.50"))
    assert (plano.data_inicio, plano.data_fim) == (date(2026, 8, 3), date(2026, 8, 31))
    assert (plano.dias_semana, plano.horario, plano.frequencia_semanal, plano.duracao) == ([0], time(14, 0), 1, "mensal")
    assert plano.observacao == "Prefere tarde"
    assert plano.criado_em == AGORA_UTC.replace(tzinfo=None)

    sessoes = db.query(Sessao).order_by(Sessao.data_hora).all()
    assert [s.data_hora for s in sessoes] == SEGUNDAS_DE_AGOSTO
    assert all(s.status == StatusSessao.PENDENTE_PAGAMENTO and s.foi_remarcada is False for s in sessoes)
    assert all(s.plano_id == plano.id for s in sessoes)


def test_valores_adulterados_no_formulario_sao_ignorados(client, db, carla, pedro, maria_logada):
    dados = _form(pedro, valor_total="1.00", quantidade="1", valor_sessao="0.01", data_fim="2026-08-03",
                  especialidade_id="999", status="encerrado")
    _confirmar(client, carla, dados)

    plano = db.query(Plano).one()
    assert (plano.desconto_percentual, plano.valor_desconto, plano.valor_total) == (5, Decimal("37.50"), Decimal("712.50"))
    assert plano.data_fim == date(2026, 8, 31) and plano.especialidade_id == carla.especialidade.id
    assert plano.status == StatusPlano.ATIVO
    assert db.query(Sessao).count() == 5


def test_valor_usa_o_valor_atual_da_especialidade(client, db, fono, carla, pedro, maria_logada):
    fono.valor_sessao = Decimal("199.90")
    db.commit()
    _confirmar(client, carla, _form(pedro))
    # RN11: 5 x R$ 199,90 = R$ 999,50; 5% = R$ 49,975, arredondado para R$ 49,98
    plano = db.query(Plano).one()
    assert (plano.valor_desconto, plano.valor_total) == (Decimal("49.98"), Decimal("949.52"))


def test_feriados_ficam_fora_das_sessoes(client, db, carla, pedro, maria_logada):
    _confirmar(client, carla, _form(pedro, duracao="bimestral"))

    plano = db.query(Plano).one()
    datas = [s.data_hora.date() for s in db.query(Sessao).order_by(Sessao.data_hora)]
    assert len(datas) == 9 and date(2026, 9, 7) not in datas  # Independência do Brasil
    assert plano.data_fim == date(2026, 10, 5) and datas[-1] == plano.data_fim
    # RN11: 9 sessões reais (feriado pulado) x R$ 150 = R$ 1.350,00; bimestral tem 7% (R$ 94,50)
    assert (plano.desconto_percentual, plano.valor_desconto, plano.valor_total) == (7, Decimal("94.50"), Decimal("1255.50"))


@pytest.mark.parametrize("agora_local, inicio_esperado", [
    (datetime(2026, 8, 3, 10, 0), date(2026, 8, 3)),  # 4h exatas antes das 14:00: aceito
    (datetime(2026, 8, 3, 10, 1), date(2026, 8, 10)),  # 3h59: pula para a semana seguinte
])
def test_rn10_primeira_sessao_com_quatro_horas_de_antecedencia(
    client, db, monkeypatch, carla, pedro, maria_logada, agora_local, inicio_esperado
):
    _fixar_relogio(monkeypatch, agora_local, agora_local.replace(hour=agora_local.hour + 3, tzinfo=timezone.utc))
    _confirmar(client, carla, _form(pedro, celulas=("0-14",), duracao="quinzenal"))
    plano = db.query(Plano).one()
    assert plano.data_inicio == inicio_esperado
    assert min(s.data_hora for s in db.query(Sessao)) == datetime.combine(inicio_esperado, time(14, 0))


@pytest.mark.parametrize("celulas", [
    ("0-12",),  # almoço (RN08)
    ("5-14",),  # sábado (RN08)
    ("0-14", "2-15"),  # horas diferentes
])
def test_horarios_invalidos_nao_gravam(client, db, carla, pedro, maria_logada, celulas):
    frequencia = str(len(celulas))
    resposta = _confirmar(client, carla, _form(pedro, celulas=celulas, frequencia=frequencia))
    assert resposta.status_code == 400
    assert db.query(Plano).count() == 0


# --- Conflito e duplo envio ----------------------------------------------------------------------

def test_conflito_surgido_depois_do_resumo_nao_grava_nada(client, db, fono, carla, pedro, maria_logada):
    form = _form(pedro, observacao="Prefere tarde")
    assert client.post(f"/contratar/{carla.usuario.id}", data=form).status_code == 200  # resumo
    _ocupar_por_outro_cliente(db, carla, fono, datetime(2026, 8, 17, 14, 30))

    resposta = _confirmar(client, carla, form)

    assert resposta.status_code == 409
    html = _html(resposta)
    assert "Nada foi contratado" in html
    assert "17/08/2026 às 14:00" in html
    assert re.search(rf'<option value="{pedro.id}" selected>', html)
    assert "Prefere tarde" in html
    assert 'id="freq-1" value="1" autocomplete="off" checked' in html
    assert db.query(Plano).count() == 1  # só o da outra cliente
    assert db.query(Sessao).count() == 1


def test_tela_de_conflito_nao_repete_o_alerta_dentro_da_grade(client, db, fono, carla, pedro, maria_logada):
    form = _form(pedro)
    _ocupar_por_outro_cliente(db, carla, fono, datetime(2026, 8, 17, 14, 30))

    html = _html(_confirmar(client, carla, form))

    assert html.count("Nada foi contratado") == 1
    assert 'id="alerta-conflitos"' in html and "17/08/2026 às 14:00" in html
    assert contratacao_service.MSG_CONFLITO not in html
    assert 'alert alert-danger py-2 small' not in html  # o alerta que repetia a mensagem dentro da grade



def test_pendente_de_outro_cliente_dentro_do_prazo_tambem_conflita(client, db, fono, carla, pedro, maria_logada):
    _ocupar_por_outro_cliente(db, carla, fono, datetime(2026, 8, 10, 14, 0), StatusSessao.PENDENTE_PAGAMENTO)
    assert _confirmar(client, carla, _form(pedro)).status_code == 409
    assert db.query(Plano).count() == 1


def test_duplo_envio_redireciona_para_o_plano_ja_criado(client, db, carla, pedro, maria_logada):
    primeira = _confirmar(client, carla, _form(pedro))
    segunda = _confirmar(client, carla, _form(pedro))

    assert segunda.status_code == 303
    assert segunda.headers["location"] == primeira.headers["location"]
    assert db.query(Plano).count() == 1
    assert db.query(Sessao).count() == 5


def test_mesmo_horario_para_outro_paciente_do_cliente_ainda_conflita(client, db, carla, pedro, maria, maria_logada):
    ana = criar_paciente(db, maria.id, nome="Ana Souza")
    _confirmar(client, carla, _form(pedro))
    assert _confirmar(client, carla, _form(ana)).status_code == 409
    assert db.query(Plano).count() == 1


def test_reenvio_com_outra_duracao_nao_e_tratado_como_duplicado(client, db, carla, pedro, maria_logada):
    _confirmar(client, carla, _form(pedro, duracao="quinzenal"))
    assert _confirmar(client, carla, _form(pedro, duracao="mensal")).status_code == 409
    assert db.query(Plano).count() == 1


# --- Validações de cadastro e acesso -------------------------------------------------------------

def test_cpf_ausente_nao_grava(client, db, carla, maria, pedro, maria_logada):
    db.get(Cliente, maria.id).cpf = None
    db.commit()

    resposta = _confirmar(client, carla, _form(pedro, cpf=CPF_VALIDO))

    assert resposta.status_code == 400
    assert contratacao_service.MSG_CPF_OBRIGATORIO in resposta.text
    assert db.query(Plano).count() == 0
    assert db.get(Cliente, maria.id).cpf is None  # a confirmação não grava CPF; isso é feito no resumo


def test_sem_paciente_volta_ao_formulario(client, db, carla, pedro, maria_logada):
    resposta = _confirmar(client, carla, _form(pedro, paciente_id=""))
    assert resposta.status_code == 400
    assert contratacao_service.MSG_PACIENTE_OBRIGATORIO in resposta.text
    assert db.query(Plano).count() == 0


def test_paciente_de_outro_cliente_retorna_404(client, db, carla, maria_logada):
    outra = criar_cliente(db, email="outra@exemplo.com", nome="Outra Cliente")
    alheio = criar_paciente(db, outra.id, nome="Paciente Alheio")
    assert _confirmar(client, carla, _form(alheio)).status_code == 404
    assert db.query(Plano).count() == 0


def test_profissional_inativo_retorna_404(client, db, carla, pedro, maria_logada):
    funcionario_service.desativar(db, carla.usuario.id, AGORA)
    assert _confirmar(client, carla, _form(pedro)).status_code == 404
    assert db.query(Plano).count() == 0


@pytest.mark.parametrize("perfil", ["admin", "funcionario"])
def test_outros_perfis_recebem_403(client, db, fono, carla, pedro, perfil):
    email = criar_admin(db).email if perfil == "admin" else carla.usuario.email
    logar(client, email)
    assert _confirmar(client, carla, _form(pedro)).status_code == 403
    assert client.get("/planos/1").status_code == 403
    assert client.get("/planos/1/renovar").status_code == 403
    assert db.query(Plano).count() == 0


def test_sem_login_redireciona_para_login(client, carla, pedro):
    for resposta in (
        _confirmar(client, carla, _form(pedro)),
        client.get("/planos/1", follow_redirects=False),
        client.get("/planos/1/renovar", follow_redirects=False),
    ):
        assert resposta.status_code == 303 and resposta.headers["location"] == "/login"


# --- Página do plano -----------------------------------------------------------------------------

def test_pagina_do_plano_mostra_aguardando_pagamento_e_prazo(client, db, carla, pedro, maria_logada):
    resposta = client.post(_url(carla), data=_form(pedro, observacao="Prefere tarde"))  # segue o redirecionamento

    assert resposta.status_code == 200
    html = _html(resposta)
    for trecho in (
        "Plano reservado", "Aguardando pagamento", "Pague até <strong>01/08/2026 às 14:00</strong>", "horário de Brasília",
        "R$ 750,00", "5 sessões", "03/08/2026 a 31/08/2026", "Carla Fono", "Pedro Souza", "Prefere tarde",
    ):
        assert trecho in html, trecho
    assert "o plano expira" in html and "cancelado" not in html
    assert f'href="/planos/{db.query(Plano).one().id}/pagamento"' in html  # "Pagar agora" (RF06, Semana 4B)
    assert "Renovar" not in html


def test_prazo_da_pagina_do_plano_e_mostrado_em_horario_local(client, db, monkeypatch, carla, pedro, maria_logada):
    # 01:30 UTC de 04/08 = 22:30 local de 03/08; o prazo vence às 02:30 local de 04/08
    _fixar_relogio(monkeypatch, datetime(2026, 8, 3, 22, 30), datetime(2026, 8, 4, 1, 30, tzinfo=timezone.utc))
    html = _html(client.post(_url(carla), data=_form(pedro)))
    assert "Pague até <strong>04/08/2026 às 02:30</strong>" in html


def test_pagina_de_plano_de_outro_cliente_retorna_404(client, db, carla, pedro, maria):
    logar(client, maria.email)
    plano_id = int(_confirmar(client, carla, _form(pedro)).headers["location"].rsplit("/", 1)[1])
    client.cookies.clear()

    logar(client, criar_cliente(db, email="outra@exemplo.com", nome="Outra Cliente").email)
    assert client.get(f"/planos/{plano_id}").status_code == 404
    assert client.get("/planos/999").status_code == 404


def test_fluxo_completo_tem_no_maximo_seis_telas(client, db, carla, pedro, maria_logada):
    # RNF05: especialidades → profissionais → contratação → resumo → plano criado
    telas = [
        client.get("/especialidades"),
        client.get(f"/especialidades/{carla.especialidade.id}/profissionais"),
        client.get(f"/contratar/{carla.usuario.id}"),
        client.post(f"/contratar/{carla.usuario.id}", data=_form(pedro)),
        client.post(_url(carla), data=_form(pedro)),
    ]
    assert all(tela.status_code == 200 for tela in telas)
    assert len(telas) <= 6
    assert "Aguardando pagamento" in telas[-1].text
