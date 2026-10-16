import re
from datetime import date, datetime, time, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.cliente import Cliente
from app.models.plano import Plano, StatusPlano
from app.models.sessao import Sessao, StatusSessao
from app.routers import cliente as cliente_router
from app.routers import inicio as inicio_router
from app.services import contratacao_service, funcionario_service
from tests.fabricas import (
    criar_admin,
    criar_cliente,
    criar_especialidade,
    criar_funcionario,
    criar_paciente,
    criar_plano,
    criar_plano_com_sessao,
    logar,
)

AGENDADA = StatusSessao.AGENDADA
PENDENTE = StatusSessao.PENDENTE_PAGAMENTO
REALIZADA = StatusSessao.REALIZADA

AGORA = datetime(2026, 8, 1, 10, 0)  # sábado, horário local
AGORA_UTC = datetime(2026, 8, 1, 13, 0, tzinfo=timezone.utc)
JULHO = [(datetime(2026, 7, dia, 14, 0), REALIZADA) for dia in (6, 13, 20, 27)]  # segundas, 14:00


@pytest.fixture(autouse=True)
def relogio_fixo(monkeypatch):
    monkeypatch.setattr(cliente_router, "agora_local", lambda: AGORA)
    monkeypatch.setattr(cliente_router, "agora_utc", lambda: AGORA_UTC)
    monkeypatch.setattr(inicio_router, "agora_local", lambda: AGORA)


@pytest.fixture
def fono(db):
    return criar_especialidade(db, "Fonoaudiologia", "150.00")


@pytest.fixture
def carla(db, fono):
    return criar_funcionario(db, fono, email="carla@exemplo.com", nome="Carla Fono")


@pytest.fixture
def bruno(db, fono):
    return criar_funcionario(db, fono, email="bruno@exemplo.com", nome="Bruno Fono")


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
def maria_logada(client, maria):
    logar(client, maria.email)
    return maria


@pytest.fixture
def encerrado(db, pedro, carla):
    return criar_plano(db, pedro, carla, JULHO, valor_total="600.00")


def _html(resposta) -> str:
    return " ".join(resposta.text.split())


def _renovar(client, plano, **params):
    return client.get(f"/planos/{plano.id}/renovar", params=params)


def _form_direto(html: str) -> tuple[str, list[tuple[str, str]]]:
    form = re.search(r'<form method="post" action="([^"]+)" id="form-renovar-direto">(.*?)</form>', html, re.S)
    campos = re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', form.group(2))
    return form.group(1), campos


def _snapshot(db, plano_id):
    plano = db.get(Plano, plano_id)
    db.refresh(plano)
    sessoes = db.execute(select(Sessao.data_hora, Sessao.status).where(Sessao.plano_id == plano_id).order_by(Sessao.data_hora)).all()
    return (plano.status, plano.valor_total, plano.data_inicio, plano.data_fim, plano.dias_semana, plano.horario, sessoes)


# --- Acesso e elegibilidade ----------------------------------------------------------------------

def test_plano_de_outro_cliente_retorna_404(client, db, encerrado):
    logar(client, criar_cliente(db, email="outra@exemplo.com", nome="Outra Cliente").email)
    assert _renovar(client, encerrado).status_code == 404
    assert client.get("/planos/999/renovar").status_code == 404


@pytest.mark.parametrize("perfil", ["admin", "funcionario"])
def test_outros_perfis_recebem_403(client, db, carla, encerrado, perfil):
    logar(client, criar_admin(db).email if perfil == "admin" else carla.usuario.email)
    assert _renovar(client, encerrado).status_code == 403


@pytest.mark.parametrize("sessoes, criado_em", [
    ([(datetime(2026, 7, 27, 14, 0), REALIZADA), (datetime(2026, 8, 3, 14, 0), AGENDADA)], datetime(2026, 7, 1, 12, 0)),
    ([(datetime(2026, 8, 1, 9, 0), REALIZADA)], datetime(2026, 7, 1, 12, 0)),  # última sessão hoje
    ([(datetime(2026, 7, 27, 14, 0), PENDENTE)], datetime(2026, 8, 1, 12, 0)),  # aguardando pagamento
])
def test_so_plano_encerrado_pode_ser_renovado(client, db, pedro, carla, maria_logada, sessoes, criado_em):
    plano = criar_plano(db, pedro, carla, sessoes, criado_em=criado_em)
    resposta = _renovar(client, plano)

    assert resposta.status_code == 409
    assert "A renovação fica disponível quando o plano for encerrado" in resposta.text
    assert 'id="form-renovar-direto"' not in resposta.text
    assert db.query(Plano).count() == 1


# --- Pré-preenchimento ---------------------------------------------------------------------------

def test_renovacao_abre_a_contratacao_pre_preenchida(client, db, pedro, carla, encerrado, maria_logada):
    resposta = _renovar(client, encerrado)

    assert resposta.status_code == 200
    html = _html(resposta)
    assert "Renovar plano" in html and "Renovação do plano de Pedro Souza" in html
    assert "Renovar com a mesma configuração" in html
    # Formulário de ajuste com paciente, frequência, duração e horário do plano anterior
    assert f'action="/contratar/{carla.usuario.id}"' in html
    assert re.search(rf'<option value="{pedro.id}" selected>', html)
    assert 'id="freq-1" value="1" autocomplete="off" checked' in html
    assert '<option value="mensal" selected>' in html
    assert re.search(r'value="0-14"[^>]*data-livre="1"[^>]*checked', html)
    # Simulação já calculada no servidor: 5 segundas de agosto
    nova = re.search(r'id="renovacao-nova">(.*?)</form>', html).group(1)
    assert "Carla Fono" in nova and "03/08/2026 a 31/08/2026" in nova and "R$ 750,00" in nova

    action, campos = _form_direto(resposta.text)
    assert action == f"/contratar/{carla.usuario.id}/confirmar"
    assert sorted(campos) == sorted([
        ("paciente_id", str(pedro.id)), ("frequencia_semanal", "1"), ("duracao", "mensal"),
        ("observacao", ""), ("celula", "0-14"),
    ])


def test_um_clique_renova_com_a_mesma_configuracao(client, db, pedro, carla, encerrado, maria_logada):
    antes = _snapshot(db, encerrado.id)
    action, campos = _form_direto(_renovar(client, encerrado).text)
    dados = {}
    for nome, valor in campos:
        dados.setdefault(nome, []).append(valor)

    resposta = client.post(action, data=dados, follow_redirects=False)

    novo = db.scalars(select(Plano).where(Plano.id != encerrado.id)).one()
    assert resposta.status_code == 303 and resposta.headers["location"] == f"/planos/{novo.id}"
    assert (novo.paciente_id, novo.funcionario_id, novo.dias_semana, novo.horario) == (pedro.id, carla.usuario.id, [0], time(14, 0))
    assert (novo.data_inicio, novo.data_fim, novo.valor_total) == (date(2026, 8, 3), date(2026, 8, 31), Decimal("750.00"))
    assert {s.status for s in db.query(Sessao).filter(Sessao.plano_id == novo.id)} == {PENDENTE}
    assert _snapshot(db, encerrado.id) == antes  # plano anterior intacto


def test_valor_recalculado_com_o_valor_atual_da_especialidade(client, db, fono, carla, encerrado, maria_logada):
    fono.valor_sessao = Decimal("200.00")
    db.commit()

    html = _html(_renovar(client, encerrado))
    assert "R$ 1.000,00" in html and "R$ 600,00" in html  # novo total e o total do plano anterior

    action, campos = _form_direto(_renovar(client, encerrado).text)
    client.post(action, data={nome: valor for nome, valor in campos})
    novo = db.scalars(select(Plano).where(Plano.id != encerrado.id)).one()
    assert novo.valor_total == Decimal("1000.00")
    assert db.get(Plano, encerrado.id).valor_total == Decimal("600.00")


def test_ajuste_passa_pelo_resumo_e_pela_mesma_confirmacao(client, db, pedro, carla, encerrado, maria_logada):
    form = {"paciente_id": str(pedro.id), "celula": ["0-14", "2-14"], "frequencia_semanal": "2",
            "duracao": "quinzenal", "observacao": ""}
    resumo = client.post(f"/contratar/{carla.usuario.id}", data=form)
    assert resumo.status_code == 200 and "4 sessões" in resumo.text

    client.post(f"/contratar/{carla.usuario.id}/confirmar", data=form)
    novo = db.scalars(select(Plano).where(Plano.id != encerrado.id)).one()
    assert (novo.frequencia_semanal, novo.duracao, novo.dias_semana) == (2, "quinzenal", [0, 2])
    assert db.get(Plano, encerrado.id).status == StatusPlano.ATIVO  # a coluna do anterior não é tocada


# --- Profissional ou horário indisponível --------------------------------------------------------

def test_profissional_desativado_pede_outra_escolha(client, db, carla, bruno, encerrado, maria_logada):
    funcionario_service.desativar(db, carla.usuario.id, AGORA)

    resposta = _renovar(client, encerrado)

    assert resposta.status_code == 200
    html = _html(resposta)
    assert "Carla Fono não está mais disponível para novos planos de Fonoaudiologia" in html
    assert 'id="form-renovar-direto"' not in html and 'id="form-contratacao"' not in html
    assert f'href="/planos/{encerrado.id}/renovar?profissional={bruno.usuario.id}"' in html


def test_escolher_outro_profissional_mantem_a_configuracao(client, db, pedro, carla, bruno, encerrado, maria_logada):
    funcionario_service.desativar(db, carla.usuario.id, AGORA)

    resposta = _renovar(client, encerrado, profissional=bruno.usuario.id)

    html = _html(resposta)
    assert resposta.status_code == 200
    assert "Renovar com a mesma configuração" in html and "Bruno Fono" in html
    assert re.search(r'value="0-14"[^>]*data-livre="1"[^>]*checked', html)
    action, _ = _form_direto(resposta.text)
    assert action == f"/contratar/{bruno.usuario.id}/confirmar"


def test_sem_outro_profissional_mostra_caminho_alternativo(client, db, carla, encerrado, maria_logada):
    funcionario_service.desativar(db, carla.usuario.id, AGORA)
    html = _html(_renovar(client, encerrado))
    assert "não há outro profissional de Fonoaudiologia disponível" in html
    assert 'href="/especialidades"' in html


def test_profissional_de_outra_especialidade_nao_e_aceito(client, db, carla, encerrado, maria_logada):
    psico = criar_funcionario(db, criar_especialidade(db, "Psicologia"), email="psi@exemplo.com", nome="Paula Psi")
    html = _html(_renovar(client, encerrado, profissional=psico.usuario.id))
    assert "não está mais disponível para novos planos de Fonoaudiologia" in html
    assert 'id="form-renovar-direto"' not in html


def test_horario_ocupado_informa_e_pede_novo_horario(client, db, fono, carla, encerrado, maria_logada):
    criar_plano_com_sessao(db, carla.usuario.id, fono, AGENDADA, data_hora=datetime(2026, 8, 17, 14, 0),
                           email_cliente="joana@exemplo.com")

    resposta = _renovar(client, encerrado)

    html = _html(resposta)
    assert resposta.status_code == 200
    assert "O horário do plano anterior (Segunda, às 14:00) não está mais livre com Carla Fono" in html
    assert 'id="form-renovar-direto"' not in html
    assert 'id="form-contratacao"' in html  # o fluxo continua: escolher outro horário
    assert not re.search(r'name="celula"[^>]*checked', html)


def test_configuracao_antiga_fora_das_duracoes_atuais(client, db, carla, encerrado, maria_logada):
    encerrado.duracao = "2 meses"
    db.commit()
    html = _html(_renovar(client, encerrado))
    assert contratacao_service.MSG_CONFIGURACAO_ANTIGA in html
    assert 'id="form-contratacao"' in html and 'id="form-renovar-direto"' not in html


# --- Início do Cliente ---------------------------------------------------------------------------

def test_inicio_mostra_renovar_so_no_plano_encerrado(client, db, pedro, carla, encerrado, maria_logada):
    ativo = criar_plano(db, pedro, carla, [(datetime(2026, 8, 4, 9, 0), AGENDADA)])
    aguardando = criar_plano(db, pedro, carla, [(datetime(2026, 8, 5, 9, 0), PENDENTE)], criado_em=datetime(2026, 8, 1, 12, 0))

    html = _html(client.get("/"))

    assert html.count(">Encerrado</span>") == 1
    assert html.count("bi-arrow-repeat") == 1
    assert f'href="/planos/{encerrado.id}/renovar"' in html
    assert f'href="/planos/{ativo.id}/renovar"' not in html
    assert f'href="/planos/{aguardando.id}/renovar"' not in html
    assert f'href="/planos/{aguardando.id}"' in html


def test_pagina_do_plano_encerrado_oferece_renovar(client, db, encerrado, maria_logada):
    html = _html(client.get(f"/planos/{encerrado.id}"))
    assert ">Encerrado</span>" in html
    assert f'href="/planos/{encerrado.id}/renovar"' in html
