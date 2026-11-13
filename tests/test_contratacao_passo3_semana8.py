import re
from datetime import date, datetime, timedelta, timezone

import pytest

from app.models.cliente import Cliente
from app.models.plano import Plano
from app.models.sessao import Sessao, StatusSessao
from app.routers import cliente as cliente_router
from app.services import contratacao_service as servico
from tests.fabricas import criar_cliente, criar_especialidade, criar_funcionario, criar_paciente, criar_plano, logar

AGORA = datetime(2026, 8, 3, 10, 0)  # segunda, horário local
CPF = "529.982.247-25"


def _relogio(monkeypatch, agora_local):
    monkeypatch.setattr(cliente_router, "agora_local", lambda: agora_local)
    monkeypatch.setattr(cliente_router, "agora_utc", lambda: (agora_local + timedelta(hours=3)).replace(tzinfo=timezone.utc))


@pytest.fixture(autouse=True)
def relogio_fixo(monkeypatch):
    _relogio(monkeypatch, AGORA)


@pytest.fixture
def carla(db):
    return criar_funcionario(db, criar_especialidade(db, "Psicopedagogia", "150.00"), email="carla@exemplo.com", nome="Carla Mendes")


@pytest.fixture
def maria(client, db):
    cliente = criar_cliente(db, email="maria@exemplo.com", nome="Maria Souza")
    db.get(Cliente, cliente.id).cpf = CPF
    db.commit()
    logar(client, cliente.email)
    return cliente


@pytest.fixture
def pedro(db, maria):
    return criar_paciente(db, maria.id, nome="Pedro Souza")


def _form(pedro, celulas=("4-14",), frequencia="1", duracao="mensal", observacao=""):
    return {"paciente_id": str(pedro.id), "celula": list(celulas), "frequencia_semanal": frequencia,
            "duracao": duracao, "observacao": observacao}


def _html(resposta) -> str:
    return " ".join(resposta.text.split())


def _revisao(client, carla, pedro, **kw):
    resposta = client.post(f"/contratar/{carla.usuario.id}", data=_form(pedro, **kw))
    assert resposta.status_code == 200
    return _html(resposta)


# --- Funções puras ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("momento, agora, esperado", [
    (datetime(2026, 8, 3, 14, 0), AGORA, "hoje, às 14:00"),
    (datetime(2026, 8, 4, 2, 0), datetime(2026, 8, 3, 22, 0), "amanhã, às 02:00"),
    (datetime(2026, 8, 6, 23, 59), AGORA, "06/08, às 23:59"),
    (datetime(2027, 1, 1, 0, 30), datetime(2026, 12, 31, 21, 0), "amanhã, às 00:30"),
])
def test_texto_dia_hora(momento, agora, esperado):
    assert servico.texto_dia_hora(momento, agora) == esperado


@pytest.mark.parametrize("prazo, esperado", [
    (datetime(2026, 8, 3, 13, 40), "3h 40min"),
    (datetime(2026, 8, 3, 10, 1), "1min"),
    (datetime(2026, 8, 3, 10, 0), None),  # exatamente no prazo: nada a contar
    (datetime(2026, 8, 3, 9, 0), None),  # vencido: nunca contagem negativa
])
def test_tempo_restante(prazo, esperado):
    assert servico.tempo_restante(prazo, AGORA) == esperado


# --- Revisão -----------------------------------------------------------------------------------------------

def test_revisao_com_seis_linhas(client, db, carla, pedro):
    html = _revisao(client, carla, pedro)
    assert "Confira e confirme</h1>" in html
    dados = re.search(r'<dl class="lista-dados rev-dados">(.*?)</dl>', html).group(1)
    assert re.findall(r"<dt>([^<]+)</dt>", dados) == [
        "Pessoa atendida", "Especialidade", "Profissional", "Dias e horário", "Frequência e duração", "Período",
    ]
    assert "<dd>1x por semana · Mensal</dd>" in dados
    assert "<dd>07/08/2026 a 04/09/2026 · 5 sessões</dd>" in dados
    assert "Sexta, às 14:00 (60 minutos)" in dados


def test_observacao_continua_depois_das_seis(client, db, carla, pedro):
    html = _revisao(client, carla, pedro, observacao="Prefere tarde")
    dados = re.search(r'<dl class="lista-dados rev-dados">(.*?)</dl>', html).group(1)
    assert re.findall(r"<dt>([^<]+)</dt>", dados)[-1] == "Observação" and "Prefere tarde" in dados


def test_datas_em_details_aberto_com_o_feriado(client, db, carla, pedro):
    html = _revisao(client, carla, pedro, celulas=("0-14",), duracao="bimestral")
    detalhes = re.search(r'<details class="rev-datas" open>.*?</details>', html).group(0)
    assert "<summary>Ver as datas</summary>" in detalhes and detalhes.count("<li>") >= 9
    assert "Feriados nacionais pulados" in detalhes and "07/09/2026 — Independência do Brasil" in detalhes


def test_prazo_pix_hoje_e_boleto_presente(client, db, carla, pedro):
    html = _revisao(client, carla, pedro)  # sexta 07/08: boleto vence no 2º dia útil antes, 05/08
    prazo = re.search(r'<div class="rev-prazo" role="note" id="aviso-prazo">.*?</div>', html).group(0)
    assert "<li>Pix: pague até hoje, às 14:00 (4 horas).</li>" in prazo
    assert "<li>Boleto: vence em 05/08/2026.</li>" in prazo
    assert html.index('id="aviso-prazo"') < html.index("Confirmar contratação") < html.index("Voltar e editar")


def test_prazo_pix_amanha(client, db, carla, pedro, monkeypatch):
    _relogio(monkeypatch, datetime(2026, 8, 3, 22, 0))
    html = _revisao(client, carla, pedro, celulas=("4-14",))
    assert "Pix: pague até amanhã, às 02:00 (4 horas)." in html


def test_boleto_ausente_mostra_a_explicacao_de_hoje(client, db, carla, pedro):
    html = _revisao(client, carla, pedro, celulas=("1-14",))  # terça 04/08: sem tempo para o boleto
    assert "Boleto: indisponível para este plano, porque não haveria tempo de compensar antes da primeira sessão." in html
    assert "Boleto: vence em" not in html


def test_voltar_e_editar_preserva_as_escolhas(client, db, carla, pedro):
    html = _revisao(client, carla, pedro, celulas=("4-14",), duracao="quinzenal")
    volta = re.search(r'<a href="([^"]+)" class="btn btn-outline-primary w-100 mt-2">', html).group(1).replace("&amp;", "&")
    assert volta == f"/contratar/{carla.usuario.id}?paciente_id={pedro.id}&frequencia_semanal=1&duracao=quinzenal&observacao=&celula=4-14"


# --- Conflito e duplo envio -----------------------------------------------------------------------------

def _ocupar(db, carla, quando):
    outro = criar_paciente(db, criar_cliente(db, email="outra@exemplo.com").id, nome="Outro")
    criar_plano(db, outro, carla, [(quando, StatusSessao.AGENDADA)])


def test_conflito_mostra_o_bloco_vermelho_e_nada_grava(client, db, carla, pedro):
    form = _form(pedro, duracao="quinzenal")
    assert client.post(f"/contratar/{carla.usuario.id}", data=form).status_code == 200
    _ocupar(db, carla, datetime(2026, 8, 14, 14, 30))
    planos_antes, sessoes_antes = db.query(Plano).count(), db.query(Sessao).count()
    resposta = client.post(f"/contratar/{carla.usuario.id}/confirmar", data=form, follow_redirects=False)
    assert resposta.status_code == 409
    html = _html(resposta)
    assert '<p class="rev-conflito-titulo">Esse horário foi ocupado enquanto você conferia. Nada foi contratado.</p>' in html
    assert "14/08/2026 às 14:00" in html
    assert ">Escolher outro horário</a>" in html and "Confirmar contratação" not in html and "Voltar e editar" not in html
    assert (db.query(Plano).count(), db.query(Sessao).count()) == (planos_antes, sessoes_antes)


def test_duplo_envio_leva_ao_plano_ja_criado(client, db, carla, pedro):
    form = _form(pedro)
    primeira = client.post(f"/contratar/{carla.usuario.id}/confirmar", data=form, follow_redirects=False)
    segunda = client.post(f"/contratar/{carla.usuario.id}/confirmar", data=form, follow_redirects=False)
    assert primeira.status_code == segunda.status_code == 303
    assert primeira.headers["location"] == segunda.headers["location"]
    assert db.query(Plano).count() == 1


def test_revisao_sem_javascript(client, db, carla, pedro):
    assert _revisao(client, carla, pedro).count("<script") == 1  # só o bundle do Bootstrap
