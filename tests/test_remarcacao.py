import re
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import event, func, select

from app.models.historico_remarcacao import HistoricoRemarcacao
from app.models.plano import Plano
from app.models.sessao import Sessao, StatusSessao
from app.routers import agenda as agenda_router
from app.routers import consultas as consultas_router
from app.services import consultas_service, remarcacao_service
from app.services.remarcacao_service import RemarcacaoInvalida
from tests.fabricas import (
    criar_admin,
    criar_cliente,
    criar_especialidade,
    criar_funcionario,
    criar_paciente,
    criar_plano,
    logar,
)

AGENDADA = StatusSessao.AGENDADA
PENDENTE = StatusSessao.PENDENTE_PAGAMENTO
REALIZADA = StatusSessao.REALIZADA

AGORA = datetime(2026, 11, 3, 10, 0)  # horário local, terça-feira
AGORA_UTC = datetime(2026, 11, 3, 13, 0)  # o mesmo instante em UTC, sem fuso
CRIADO_PAGO = datetime(2026, 10, 1, 12, 0)
CRIADO_NO_PRAZO = datetime(2026, 11, 3, 12, 0)  # UTC = 09:00 local; Pix até 13:00 local
CRIADO_VENCIDO = datetime(2026, 11, 3, 8, 0)  # UTC = 05:00 local; Pix venceu às 09:00 local

QUI = datetime(2026, 11, 5, 14, 0)
TER = datetime(2026, 11, 10, 14, 0)
QUI_SEGUINTE = datetime(2026, 11, 12, 14, 0)


@pytest.fixture(autouse=True)
def relogio_fixo(monkeypatch):
    monkeypatch.setattr(consultas_router, "agora_local", lambda: AGORA)
    monkeypatch.setattr(consultas_router, "agora_utc", lambda: AGORA_UTC)
    monkeypatch.setattr(agenda_router, "agora_local", lambda: AGORA)


@pytest.fixture
def fono(db):
    return criar_funcionario(db, criar_especialidade(db, "Fonoaudiologia"))


@pytest.fixture
def psico(db):
    return criar_funcionario(db, criar_especialidade(db, "Psicologia"), email="psi@exemplo.com", nome="Paula Psi")


@pytest.fixture
def cliente(db):
    return criar_cliente(db)


@pytest.fixture
def paciente(db, cliente):
    return criar_paciente(db, cliente.id, nome="Pedro Souza")


@pytest.fixture
def plano(db, paciente, fono):
    return criar_plano(db, paciente, fono, [(QUI, AGENDADA), (TER, AGENDADA), (QUI_SEGUINTE, AGENDADA)], criado_em=CRIADO_PAGO)


@pytest.fixture
def logado(client, cliente):
    logar(client, cliente.email)
    return client


def _sessao(db, plano, data_hora) -> Sessao:
    return db.scalar(select(Sessao).where(Sessao.plano_id == plano.id, Sessao.data_hora == data_hora))


def _ocupar(db, funcionario, data_hora, status=AGENDADA, criado_em=CRIADO_PAGO, email="outro@exemplo.com"):
    outro = criar_cliente(db, email=email)
    return criar_plano(db, criar_paciente(db, outro.id, nome="Outro Atendido"), funcionario, [(data_hora, status)], criado_em=criado_em)


def _historicos(db) -> list[HistoricoRemarcacao]:
    return list(db.scalars(select(HistoricoRemarcacao).order_by(HistoricoRemarcacao.id)))


def _html(resposta) -> str:
    return " ".join(resposta.text.split())


def _remarcar(client, sessao_id, novo: datetime | str):
    valor = novo.isoformat(timespec="minutes") if isinstance(novo, datetime) else novo
    return client.post(f"/consultas/{sessao_id}/remarcar", data={"novo_horario": valor}, follow_redirects=False)


# --- Regras puras (RNF14) --------------------------------------------------------------------------

def test_rn02_exatamente_4_horas_pode_e_3h59_nao():
    assert remarcacao_service.motivo_bloqueio(AGENDADA, AGORA + timedelta(hours=4), AGORA) is None
    assert remarcacao_service.motivo_bloqueio(AGENDADA, AGORA + timedelta(hours=3, minutes=59), AGORA) == "Remarcação indisponível: menos de 4 h para a consulta"
    with pytest.raises(RemarcacaoInvalida) as erro:
        remarcacao_service.validar_sessao_remarcavel(AGENDADA, datetime(2026, 11, 3, 13, 59), AGORA)
    assert "pelo menos 4 horas de antecedência" in erro.value.mensagem
    assert "03/11/2026 às 09:59" in erro.value.mensagem


@pytest.mark.parametrize("status, data_hora, motivo", [
    (PENDENTE, QUI, "Após o pagamento"),
    (REALIZADA, datetime(2026, 11, 2, 14, 0), "Consulta já realizada"),
    (AGENDADA, datetime(2026, 11, 2, 14, 0), "Horário já passou"),
    (AGENDADA, AGORA, "Horário já passou"),
    (AGENDADA, QUI, None),
])
def test_motivo_de_bloqueio(status, data_hora, motivo):
    assert remarcacao_service.motivo_bloqueio(status, data_hora, AGORA) == motivo


@pytest.mark.parametrize("novo, trecho", [
    (QUI, "já está marcada neste horário"),
    (datetime(2026, 11, 3, 9, 0), "já passou"),
    (AGORA, "já passou"),
    (datetime(2027, 1, 4, 9, 0), "próximos 60 dias"),
    (datetime(2026, 11, 7, 9, 0), "segunda a sexta"),
    (datetime(2026, 11, 15, 9, 0), "segunda a sexta"),  # feriado em domingo: cai antes no fim de semana
    (datetime(2026, 11, 6, 12, 0), "hora cheia"),
    (datetime(2026, 11, 6, 18, 0), "hora cheia"),
    (datetime(2026, 11, 6, 9, 30), "hora cheia"),
    (datetime(2026, 11, 20, 9, 0), "20/11/2026 é feriado nacional"),
])
def test_novo_horario_invalido(novo, trecho):
    with pytest.raises(RemarcacaoInvalida) as erro:
        remarcacao_service.validar_novo_horario(QUI, novo, AGORA)
    assert trecho in erro.value.mensagem


@pytest.mark.parametrize("novo", [
    datetime(2026, 11, 3, 11, 0),  # hoje, 1h depois: a remarcação não tem a antecedência de 4h da RN10
    datetime(2026, 11, 6, 8, 0),
    datetime(2026, 11, 6, 17, 0),
])
def test_novo_horario_valido(novo):
    remarcacao_service.validar_novo_horario(QUI, novo, AGORA)


def test_janela_de_60_dias():
    agora = datetime(2026, 11, 6, 10, 0)  # sexta; o 60º dia é terça, 05/01/2027
    assert remarcacao_service.janela(agora) == (date(2026, 11, 6), date(2027, 1, 5))
    remarcacao_service.validar_novo_horario(QUI_SEGUINTE, datetime(2027, 1, 5, 9, 0), agora)
    with pytest.raises(RemarcacaoInvalida):
        remarcacao_service.validar_novo_horario(QUI_SEGUINTE, datetime(2027, 1, 6, 9, 0), agora)


# --- Horários livres (RN01, RN06, RN08) -----------------------------------------------------------

def _resumo(db, cliente, plano, data_hora):
    return remarcacao_service.buscar_sessao_do_cliente(db, cliente.id, _sessao(db, plano, data_hora).id)


def test_horarios_livres_exclui_ocupados_por_sobreposicao(db, fono, cliente, plano):
    _ocupar(db, fono, datetime(2026, 11, 6, 9, 0))
    _ocupar(db, fono, datetime(2026, 11, 6, 14, 30), email="parcial@exemplo.com")  # dado legado fora da hora cheia
    livres = remarcacao_service.horarios_livres(db, _resumo(db, cliente, plano, QUI), date(2026, 11, 6), AGORA)
    horas = [h.hour for h in livres.horarios]
    assert horas == [8, 10, 11, 16, 17]  # 9h ocupado; 14h e 15h sobrepõem parcialmente 14h30; 8h e 10h são adjacentes
    assert livres.motivo is None


def test_horarios_livres_ignora_a_propria_sessao_e_o_horario_atual(db, fono, cliente, paciente):
    plano = criar_plano(db, paciente, fono, [(datetime(2026, 11, 6, 14, 30), AGENDADA)], criado_em=CRIADO_PAGO)
    resumo = _resumo(db, cliente, plano, datetime(2026, 11, 6, 14, 30))
    horas = [h.hour for h in remarcacao_service.horarios_livres(db, resumo, date(2026, 11, 6), AGORA).horarios]
    assert 14 in horas and 15 in horas  # a sessão sendo remarcada não bloqueia a si mesma


def test_horario_atual_nao_aparece_como_opcao(db, cliente, plano):
    horas = [h.hour for h in remarcacao_service.horarios_livres(db, _resumo(db, cliente, plano, QUI), QUI.date(), AGORA).horarios]
    assert 14 not in horas and 15 in horas


def test_horarios_de_hoje_so_os_que_ainda_nao_passaram(db, cliente, plano):
    horas = [h.hour for h in remarcacao_service.horarios_livres(db, _resumo(db, cliente, plano, QUI), AGORA.date(), AGORA).horarios]
    assert horas == [11, 14, 15, 16, 17]


def test_pendente_vencida_nao_bloqueia_e_no_prazo_bloqueia(db, fono, cliente, plano):
    _ocupar(db, fono, datetime(2026, 11, 6, 9, 0), PENDENTE, CRIADO_VENCIDO)
    _ocupar(db, fono, datetime(2026, 11, 6, 10, 0), PENDENTE, CRIADO_NO_PRAZO, email="prazo@exemplo.com")
    horas = [h.hour for h in remarcacao_service.horarios_livres(db, _resumo(db, cliente, plano, QUI), date(2026, 11, 6), AGORA).horarios]
    assert 9 in horas and 10 not in horas


def test_sessao_de_outro_profissional_nao_bloqueia(db, psico, cliente, plano):
    _ocupar(db, psico, datetime(2026, 11, 6, 9, 0))
    horas = [h.hour for h in remarcacao_service.horarios_livres(db, _resumo(db, cliente, plano, QUI), date(2026, 11, 6), AGORA).horarios]
    assert 9 in horas


@pytest.mark.parametrize("dia, motivo", [
    (date(2026, 11, 20), "Feriado nacional"),
    (date(2026, 11, 7), "Fim de semana"),
    (date(2026, 11, 2), "Fora da janela de 60 dias"),
    (date(2027, 1, 3), "Fora da janela de 60 dias"),
])
def test_dia_sem_horarios_com_motivo(db, cliente, plano, dia, motivo):
    livres = remarcacao_service.horarios_livres(db, _resumo(db, cliente, plano, QUI), dia, AGORA)
    assert (livres.horarios, livres.motivo) == ([], motivo)


def test_dia_lotado_sem_horarios_livres(db, fono, cliente, plano):
    for indice, hora in enumerate([8, 9, 10, 11, 14, 15, 16, 17]):
        _ocupar(db, fono, datetime(2026, 11, 6, hora, 0), email=f"lotado{indice}@exemplo.com")
    livres = remarcacao_service.horarios_livres(db, _resumo(db, cliente, plano, QUI), date(2026, 11, 6), AGORA)
    assert (livres.horarios, livres.motivo) == ([], "Sem horários livres")


# --- Endpoint JSON --------------------------------------------------------------------------------

def test_formato_do_json(db, logado, plano):
    sessao = _sessao(db, plano, QUI)
    resposta = logado.get(f"/consultas/{sessao.id}/horarios", params={"data": "2026-11-06"})
    assert resposta.status_code == 200
    assert resposta.json() == {
        "data": "2026-11-06",
        "horarios": [{"valor": f"2026-11-06T{h:02d}:00", "rotulo": f"{h:02d}:00"} for h in [8, 9, 10, 11, 14, 15, 16, 17]],
        "motivo": None,
    }


def test_json_dia_feriado(db, logado, plano):
    resposta = logado.get(f"/consultas/{_sessao(db, plano, QUI).id}/horarios", params={"data": "2026-11-20"})
    assert resposta.json() == {"data": "2026-11-20", "horarios": [], "motivo": "Feriado nacional"}


@pytest.mark.parametrize("data", ["", "abc", "2026-02-30", "06/11/2026"])
def test_json_data_invalida_da_400_com_mensagem(db, logado, plano, data):
    resposta = logado.get(f"/consultas/{_sessao(db, plano, QUI).id}/horarios", params={"data": data})
    assert resposta.status_code == 400
    assert resposta.json() == {"erro": consultas_router.MSG_DATA_INVALIDA}


def test_json_de_sessao_nao_remarcavel_da_409(db, logado, paciente, fono):
    plano = criar_plano(db, paciente, fono, [(QUI, PENDENTE)], criado_em=CRIADO_NO_PRAZO)
    resposta = logado.get(f"/consultas/{_sessao(db, plano, QUI).id}/horarios", params={"data": "2026-11-06"})
    assert resposta.status_code == 409 and "aguarda o pagamento" in resposta.json()["erro"]


# --- Acesso ---------------------------------------------------------------------------------------

def test_sessao_de_outro_cliente_ou_inexistente_da_404(client, db, plano):
    intruso = criar_cliente(db, email="intruso@exemplo.com")
    logar(client, intruso.email)
    sessao_id = _sessao(db, plano, QUI).id
    for alvo in (sessao_id, 99999):
        assert client.get(f"/consultas/{alvo}/remarcar").status_code == 404
        assert client.get(f"/consultas/{alvo}/horarios", params={"data": "2026-11-06"}).status_code == 404
        assert _remarcar(client, alvo, datetime(2026, 11, 6, 9, 0)).status_code == 404
    assert _sessao(db, plano, QUI) is not None and _historicos(db) == []


@pytest.mark.parametrize("perfil", ["funcionario", "admin"])
def test_profissional_e_admin_sao_bloqueados(client, db, fono, plano, perfil):
    usuario = fono.usuario if perfil == "funcionario" else criar_admin(db)
    logar(client, usuario.email)
    sessao_id = _sessao(db, plano, QUI).id
    assert client.get("/consultas").status_code == 403
    assert client.get(f"/consultas/{sessao_id}/remarcar").status_code == 403
    assert client.get(f"/consultas/{sessao_id}/horarios", params={"data": "2026-11-06"}).status_code == 403
    assert _remarcar(client, sessao_id, datetime(2026, 11, 6, 9, 0)).status_code == 403


def test_visitante_vai_para_o_login(client):
    resposta = client.get("/consultas", follow_redirects=False)
    assert resposta.status_code == 303 and resposta.headers["location"] == "/login"


# --- Confirmação ----------------------------------------------------------------------------------

def test_remarcar_atualiza_a_sessao_e_grava_historico(db, logado, plano):
    sessao = _sessao(db, plano, QUI)
    novo = datetime(2026, 11, 6, 9, 0)
    resposta = _remarcar(logado, sessao.id, novo)
    assert resposta.status_code == 303
    assert resposta.headers["location"] == f"/consultas?remarcada={sessao.id}"

    db.expire_all()
    assert db.scalar(select(func.count(Sessao.id)).where(Sessao.plano_id == plano.id)) == 3
    atualizada = db.get(Sessao, sessao.id)
    assert (atualizada.data_hora, atualizada.foi_remarcada, atualizada.status) == (novo, True, AGENDADA)
    historico, = _historicos(db)
    assert (historico.sessao_id, historico.data_hora_original, historico.data_hora_nova) == (sessao.id, QUI, novo)
    assert historico.solicitado_em == AGORA_UTC

    html = _html(logado.get(resposta.headers["location"]))
    assert "Consulta de Pedro Souza remarcada de 05/11/2026 às 14:00 para 06/11/2026 às 09:00." in html


def test_demais_sessoes_do_plano_ficam_intactas(db, logado, plano):
    _remarcar(logado, _sessao(db, plano, TER).id, datetime(2026, 11, 11, 9, 0))
    db.expire_all()
    for data_hora in (QUI, QUI_SEGUINTE):
        intacta = _sessao(db, plano, data_hora)
        assert intacta is not None and intacta.foi_remarcada is False


def test_data_fim_avanca_quando_a_ultima_vai_para_depois(db, logado, plano):
    _remarcar(logado, _sessao(db, plano, QUI_SEGUINTE).id, datetime(2026, 11, 16, 9, 0))
    db.expire_all()
    atualizado = db.get(Plano, plano.id)
    assert (atualizado.data_inicio, atualizado.data_fim) == (date(2026, 11, 5), date(2026, 11, 16))


def test_data_fim_recua_quando_a_ultima_passa_a_ser_outra(db, logado, plano):
    _remarcar(logado, _sessao(db, plano, QUI_SEGUINTE).id, datetime(2026, 11, 9, 9, 0))
    db.expire_all()
    assert db.get(Plano, plano.id).data_fim == date(2026, 11, 10)


def test_data_inicio_muda_quando_a_primeira_muda(db, logado, plano):
    _remarcar(logado, _sessao(db, plano, QUI).id, datetime(2026, 11, 4, 9, 0))
    db.expire_all()
    assert db.get(Plano, plano.id).data_inicio == date(2026, 11, 4)
    _remarcar(logado, _sessao(db, plano, datetime(2026, 11, 4, 9, 0)).id, datetime(2026, 11, 11, 9, 0))
    db.expire_all()
    assert db.get(Plano, plano.id).data_inicio == date(2026, 11, 10)


def test_conflito_na_confirmacao_da_409_e_nada_muda(db, logado, fono, plano):
    sessao = _sessao(db, plano, QUI)
    _ocupar(db, fono, datetime(2026, 11, 6, 9, 0))  # ocupado entre a escolha e a confirmação
    resposta = _remarcar(logado, sessao.id, datetime(2026, 11, 6, 9, 0))
    assert resposta.status_code == 409
    assert "Nada foi alterado" in _html(resposta)
    db.expire_all()
    assert (db.get(Sessao, sessao.id).data_hora, db.get(Sessao, sessao.id).foi_remarcada) == (QUI, False)
    assert _historicos(db) == [] and db.get(Plano, plano.id).data_inicio == date(2026, 11, 5)


def test_sobreposicao_parcial_bloqueia_na_confirmacao(db, logado, fono, plano):
    sessao = _sessao(db, plano, QUI)
    _ocupar(db, fono, datetime(2026, 11, 6, 14, 30))  # dado legado fora da hora cheia: cruza 14h e 15h
    for novo in (datetime(2026, 11, 6, 14, 0), datetime(2026, 11, 6, 15, 0)):
        assert _remarcar(logado, sessao.id, novo).status_code == 409
    db.expire_all()
    assert db.get(Sessao, sessao.id).data_hora == QUI and _historicos(db) == []


def test_horario_adjacente_e_permitido_na_confirmacao(db, logado, fono, plano):
    _ocupar(db, fono, datetime(2026, 11, 6, 9, 0))
    assert _remarcar(logado, _sessao(db, plano, QUI).id, datetime(2026, 11, 6, 10, 0)).status_code == 303


@pytest.mark.parametrize("novo, status_esperado, trecho", [
    (QUI, 400, "já está marcada neste horário"),
    (datetime(2026, 11, 3, 9, 0), 400, "já passou"),
    (datetime(2026, 11, 20, 9, 0), 400, "feriado nacional"),
    (datetime(2026, 11, 7, 9, 0), 400, "segunda a sexta"),
    (datetime(2026, 11, 6, 13, 0), 400, "hora cheia"),
    (datetime(2027, 1, 4, 9, 0), 400, "próximos 60 dias"),
    ("", 400, "Escolha um dos horários livres."),
    ("amanhã", 400, "Escolha um dos horários livres."),
])
def test_confirmacao_revalida_as_regras(db, logado, plano, novo, status_esperado, trecho):
    sessao = _sessao(db, plano, QUI)
    resposta = _remarcar(logado, sessao.id, novo)
    assert resposta.status_code == status_esperado
    assert trecho in _html(resposta)
    db.expire_all()
    assert db.get(Sessao, sessao.id).data_hora == QUI and _historicos(db) == []


@pytest.mark.parametrize("status, criado_em, trecho", [
    (PENDENTE, CRIADO_NO_PRAZO, "aguarda o pagamento"),
    (REALIZADA, CRIADO_PAGO, "já foi realizada"),
])
def test_pendente_e_realizada_nao_podem_ser_remarcadas(db, logado, paciente, fono, status, criado_em, trecho):
    plano = criar_plano(db, paciente, fono, [(QUI, status)], criado_em=criado_em)
    sessao = _sessao(db, plano, QUI)
    resposta = _remarcar(logado, sessao.id, datetime(2026, 11, 6, 9, 0))
    assert resposta.status_code == 400 and trecho in _html(resposta)
    tela = _html(logado.get(f"/consultas/{sessao.id}/remarcar"))
    assert trecho in tela and 'id="form-remarcar"' not in tela
    db.expire_all()
    assert db.get(Sessao, sessao.id).data_hora == QUI and _historicos(db) == []


def test_rn02_na_confirmacao(db, logado, paciente, fono):
    perto = datetime(2026, 11, 3, 14, 0)  # 4h depois de AGORA: ainda pode
    plano = criar_plano(db, paciente, fono, [(perto, AGENDADA), (datetime(2026, 11, 3, 13, 0), AGENDADA)], criado_em=CRIADO_PAGO)
    assert _remarcar(logado, _sessao(db, plano, perto).id, datetime(2026, 11, 6, 9, 0)).status_code == 303
    resposta = _remarcar(logado, _sessao(db, plano, datetime(2026, 11, 3, 13, 0)).id, datetime(2026, 11, 6, 10, 0))
    assert resposta.status_code == 400
    assert "pelo menos 4 horas de antecedência" in _html(resposta)


def test_duplo_envio_nao_duplica_historico(db, logado, plano):
    sessao_id = _sessao(db, plano, QUI).id
    novo = datetime(2026, 11, 6, 9, 0)
    assert _remarcar(logado, sessao_id, novo).status_code == 303
    segunda = _remarcar(logado, sessao_id, novo)
    assert segunda.status_code == 303
    assert segunda.headers["location"] == f"/consultas?remarcada={sessao_id}&repetido=1"
    assert len(_historicos(db)) == 1
    html = _html(logado.get(segunda.headers["location"]))
    assert "já estava remarcada para 06/11/2026 às 09:00. Nada foi alterado." in html and "alert-info" in html


def test_varias_remarcacoes_geram_varias_linhas_de_historico(db, logado, plano):
    sessao_id = _sessao(db, plano, QUI).id
    for novo in (datetime(2026, 11, 6, 9, 0), datetime(2026, 11, 6, 10, 0), datetime(2026, 11, 9, 8, 0)):
        assert _remarcar(logado, sessao_id, novo).status_code == 303
    historicos = _historicos(db)
    assert [(h.data_hora_original, h.data_hora_nova) for h in historicos] == [
        (QUI, datetime(2026, 11, 6, 9, 0)),
        (datetime(2026, 11, 6, 9, 0), datetime(2026, 11, 6, 10, 0)),
        (datetime(2026, 11, 6, 10, 0), datetime(2026, 11, 9, 8, 0)),
    ]


def test_post_com_id_invalido_volta_html_e_nao_json(logado):
    resposta = logado.post("/consultas/abc/remarcar", data={"novo_horario": "2026-11-06T09:00"})
    assert resposta.status_code == 400
    assert resposta.headers["content-type"].startswith("text/html")


def test_integracao_com_a_agenda_do_profissional(client, db, fono, cliente, plano):
    logar(client, cliente.email)
    _remarcar(client, _sessao(db, plano, QUI).id, datetime(2026, 11, 6, 9, 0))
    client.cookies.clear()
    logar(client, fono.usuario.email)
    original = _html(client.get("/agenda", params={"visao": "dia", "data": "2026-11-05"}))
    nova = _html(client.get("/agenda", params={"visao": "dia", "data": "2026-11-06"}))
    assert "Pedro Souza" not in original
    assert "Pedro Souza" in nova and ">Remarcada<" in nova


# --- Tela de remarcação ---------------------------------------------------------------------------

def test_tela_sem_js_mostra_horarios_pela_query(db, logado, plano):
    html = _html(logado.get(f"/consultas/{_sessao(db, plano, QUI).id}/remarcar", params={"data": "2026-11-06"}))
    assert 'min="2026-11-03" max="2027-01-02"' in html
    assert html.count('name="novo_horario"') == 8
    assert 'value="2026-11-06T09:00"' in html
    assert "js/remarcar.js" in html


def test_tela_mostra_feriado_com_nome(db, logado, plano):
    html = _html(logado.get(f"/consultas/{_sessao(db, plano, QUI).id}/remarcar", params={"data": "2026-11-20"}))
    assert "Feriado nacional (" in html and 'name="novo_horario"' not in html


def test_tela_com_data_invalida_avisa(db, logado, plano):
    html = _html(logado.get(f"/consultas/{_sessao(db, plano, QUI).id}/remarcar", params={"data": "xx"}))
    assert "Informe uma data válida." in html


# --- Lista de consultas ---------------------------------------------------------------------------

def test_lista_proximas_com_status_botao_e_motivos(db, logado, paciente, fono, plano):
    criar_plano(db, paciente, fono, [(datetime(2026, 11, 4, 9, 0), PENDENTE)], criado_em=CRIADO_NO_PRAZO)
    criar_plano(db, paciente, fono, [(datetime(2026, 11, 3, 13, 0), AGENDADA)], criado_em=CRIADO_PAGO)
    sessao = _sessao(db, plano, TER)
    sessao.foi_remarcada = True
    db.commit()

    html = _html(logado.get("/consultas"))
    datas = re.findall(r'consulta-data">\w+, <span class="font-monospace-num">([\d/]+)</span></span> <span[^>]*>(\d\d:\d\d)', html)
    assert datas == [("03/11/2026", "13:00"), ("04/11/2026", "09:00"), ("05/11/2026", "14:00"),
                     ("10/11/2026", "14:00"), ("12/11/2026", "14:00")]
    assert "Remarcação indisponível: menos de 4 h para a consulta" in html
    assert "Após o pagamento" in html
    assert html.count(">Remarcar</a>") == 3
    assert html.count('status-pendente">Aguardando pagamento<') == 1
    assert html.count('status-remarcada">Remarcada<') == 1
    assert "com Carlos Fono" in html and 'chip-especialidade">Fonoaudiologia<' in html


def test_lista_anteriores_em_ordem_decrescente(db, logado, paciente, fono):
    criar_plano(db, paciente, fono, [
        (datetime(2026, 10, 20, 9, 0), REALIZADA), (datetime(2026, 10, 27, 9, 0), REALIZADA),
        (datetime(2026, 11, 2, 9, 0), AGENDADA),
    ], criado_em=CRIADO_PAGO)
    html = _html(logado.get("/consultas", params={"aba": "anteriores"}))
    assert re.findall(r"(\d\d/\d\d/2026)</span></span>", html) == ["02/11/2026", "27/10/2026", "20/10/2026"]
    assert html.count("Consulta já realizada") == 2 and "Horário já passou" in html
    assert ">Remarcar</a>" not in html


def test_lista_so_tem_consultas_do_proprio_cliente(client, db, fono, plano):
    _ocupar(db, fono, datetime(2026, 11, 6, 9, 0))
    logar(client, "outro@exemplo.com")
    html = _html(client.get("/consultas"))
    assert "Outro Atendido" in html and "Pedro Souza" not in html


def test_lista_vazia(logado):
    assert "Nenhuma consulta marcada" in _html(logado.get("/consultas"))
    assert "Nenhuma consulta anterior" in _html(logado.get("/consultas", params={"aba": "anteriores"}))


def test_paginacao_de_20_por_pagina(db, logado, paciente, fono):
    inicio = datetime(2026, 11, 4, 8, 0)
    criar_plano(db, paciente, fono, [(inicio + timedelta(days=i), AGENDADA) for i in range(25)], criado_em=CRIADO_PAGO)
    primeira = _html(logado.get("/consultas"))
    assert primeira.count('class="consulta-item') == 20 and "Página 1 de 2" in primeira
    segunda = _html(logado.get("/consultas", params={"pagina": "2"}))
    assert segunda.count('class="consulta-item') == 5 and "Página 2 de 2" in segunda
    alem = _html(logado.get("/consultas", params={"pagina": "9"}))
    assert alem.count('class="consulta-item') == 5 and "Página 2 de 2" in alem


def test_lista_faz_uma_unica_consulta_ao_banco(db, cliente, fono):
    for indice in range(6):
        atendido = criar_paciente(db, cliente.id, nome=f"Atendido {indice}")
        criar_plano(db, atendido, fono, [(datetime(2026, 11, 4 + indice, 9, 0), AGENDADA)], criado_em=CRIADO_PAGO)
    cliente_id = cliente.id
    db.expire_all()
    comandos = []
    ouvinte = lambda *args: comandos.append(args[2])  # noqa: E731
    event.listen(db.get_bind(), "before_cursor_execute", ouvinte)
    try:
        pagina = consultas_service.consultas_do_cliente(db, cliente_id, AGORA)
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", ouvinte)
    assert len(pagina.consultas) == 6 and pagina.total == 6
    assert len(comandos) == 1


# --- Polimento de interface (entre as Semanas 6 e 7) ---------------------------------------------

def test_aviso_unico_de_sessoes_aguardando_pagamento_com_link_por_plano(db, logado, paciente, fono, psico, plano):
    fono_pendente = criar_plano(db, paciente, fono, [
        (datetime(2026, 11, 4, 9, 0), PENDENTE), (datetime(2026, 11, 11, 9, 0), PENDENTE),
    ], criado_em=CRIADO_NO_PRAZO)
    psico_pendente = criar_plano(db, paciente, psico, [(datetime(2026, 11, 6, 15, 0), PENDENTE)], criado_em=CRIADO_NO_PRAZO)

    html = _html(logado.get("/consultas"))
    assert html.count('id="aviso-pagamento-pendente"') == 1
    assert "3 sessões aguardando pagamento" in html
    for p in (fono_pendente, psico_pendente):
        assert html.count(f'href="/planos/{p.id}" class="btn btn-primary btn-sm">Pagar agora') == 1
    assert f'href="/planos/{plano.id}"' not in html  # plano pago não ganha link de pagamento
    assert html.count(">Após o pagamento<") == 3
    assert html.count(">Remarcar</a>") == 3  # a regra não muda: só sessão agendada é remarcável


def test_aviso_de_pagamento_no_singular_e_fora_da_aba_anteriores(db, logado, paciente, fono):
    criar_plano(db, paciente, fono, [(datetime(2026, 11, 4, 9, 0), PENDENTE)], criado_em=CRIADO_NO_PRAZO)
    assert "1 sessão aguardando pagamento" in _html(logado.get("/consultas"))
    assert "aviso-pagamento-pendente" not in _html(logado.get("/consultas", params={"aba": "anteriores"}))


def test_sem_aviso_de_pagamento_quando_nada_esta_pendente_ou_o_prazo_venceu(db, logado, paciente, fono, plano):
    assert "aviso-pagamento-pendente" not in _html(logado.get("/consultas"))
    criar_plano(db, paciente, fono, [(datetime(2026, 11, 4, 9, 0), PENDENTE)], criado_em=CRIADO_VENCIDO)
    assert "aviso-pagamento-pendente" not in _html(logado.get("/consultas"))


def test_tela_de_remarcar_mostra_o_limite_da_janela(db, logado, plano, monkeypatch):
    html = _html(logado.get(f"/consultas/{_sessao(db, plano, QUI).id}/remarcar"))
    assert "até 02/01/2027 (60 dias a partir de hoje)" in html

    monkeypatch.setattr(remarcacao_service, "JANELA_REMARCACAO_DIAS", 30)
    monkeypatch.setattr(consultas_router, "JANELA_REMARCACAO_DIAS", 30)
    monkeypatch.setattr(remarcacao_service, "janela", lambda agora: (agora.date(), agora.date() + timedelta(days=30)))
    html = _html(logado.get(f"/consultas/{_sessao(db, plano, QUI).id}/remarcar"))
    assert "até 03/12/2026 (30 dias a partir de hoje)" in html
