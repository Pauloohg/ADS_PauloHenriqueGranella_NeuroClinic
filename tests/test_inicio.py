import re
from collections import Counter
from datetime import date, datetime, timedelta

import pytest

from app.core.templating import tempo_curto
from app.models.plano import StatusPlano
from app.models.sessao import StatusSessao
from app.routers import inicio as inicio_router
from app.services import funcionario_service, inicio_service
from app.services.inicio_service import AGUARDANDO_PAGAMENTO, ATIVO, ENCERRADO
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

AGORA = datetime(2026, 3, 10, 11, 0)  # horário local (terça-feira)
CRIADO_NO_PRAZO = datetime(2026, 3, 10, 13, 0)  # UTC = 10:00 local; prazo até 14:00 local
CRIADO_VENCIDO = datetime(2026, 3, 10, 9, 0)  # UTC = 06:00 local; prazo venceu às 10:00 local


@pytest.fixture(autouse=True)
def relogio_fixo(monkeypatch):
    monkeypatch.setattr(inicio_router, "agora_local", lambda: AGORA)


@pytest.fixture
def fono(db):
    return criar_funcionario(db, criar_especialidade(db, "Fonoaudiologia"))


@pytest.fixture
def cliente(db):
    return criar_cliente(db)


@pytest.fixture
def paciente(db, cliente):
    return criar_paciente(db, cliente.id, nome="Pedro Souza")


def _html(resposta) -> str:
    return " ".join(resposta.text.split())


# --- Saudação ------------------------------------------------------------------------------------

@pytest.mark.parametrize("hora, minuto, esperado", [
    (4, 59, "Boa noite"),
    (5, 0, "Bom dia"),
    (11, 59, "Bom dia"),
    (12, 0, "Boa tarde"),
    (17, 59, "Boa tarde"),
    (18, 0, "Boa noite"),
    (0, 0, "Boa noite"),
])
def test_saudacao_por_faixa_de_horario(hora, minuto, esperado):
    assert inicio_service.saudacao(datetime(2026, 3, 10, hora, minuto)) == esperado


def test_rotulo_dia():
    hoje = date(2026, 3, 10)
    assert inicio_service.rotulo_dia(hoje, hoje) == "hoje"
    assert inicio_service.rotulo_dia(date(2026, 3, 11), hoje) == "amanhã"
    assert inicio_service.rotulo_dia(date(2026, 3, 12), hoje) is None
    assert inicio_service.rotulo_dia(date(2026, 3, 9), hoje) is None


@pytest.mark.parametrize("intervalo, esperado", [
    (timedelta(hours=3, minutes=5), "3h 05min"),
    (timedelta(hours=4), "4h"),
    (timedelta(minutes=42, seconds=30), "42min"),
    (timedelta(seconds=20), "menos de 1min"),
])
def test_tempo_curto(intervalo, esperado):
    assert tempo_curto(intervalo) == esperado


# --- Próxima sessão ------------------------------------------------------------------------------

def test_proxima_sessao_escolhe_a_agendada_futura_mais_proxima(db, fono, cliente, paciente):
    criar_plano(db, paciente, fono, [
        (datetime(2026, 3, 9, 14, 0), AGENDADA),  # passada
        (datetime(2026, 3, 10, 10, 0), AGENDADA),  # começou antes de agora
        (datetime(2026, 3, 12, 9, 0), PENDENTE),
        (datetime(2026, 3, 11, 9, 0), REALIZADA),
        (datetime(2026, 3, 17, 14, 0), AGENDADA),
        (datetime(2026, 3, 13, 15, 0), AGENDADA),
    ])

    proxima = inicio_service.proxima_sessao(db, cliente.id, AGORA)

    assert proxima.data_hora == datetime(2026, 3, 13, 15, 0)
    assert (proxima.paciente, proxima.especialidade, proxima.profissional) == ("Pedro Souza", "Fonoaudiologia", "Carlos Fono")


def test_proxima_sessao_aceita_sessao_exatamente_agora(db, fono, cliente, paciente):
    criar_plano(db, paciente, fono, [(AGORA, AGENDADA)])
    assert inicio_service.proxima_sessao(db, cliente.id, AGORA).data_hora == AGORA


def test_proxima_sessao_ignora_pendente_e_realizada(db, fono, cliente, paciente):
    criar_plano(db, paciente, fono, [(datetime(2026, 3, 12, 9, 0), PENDENTE), (datetime(2026, 3, 13, 9, 0), REALIZADA)])
    assert inicio_service.proxima_sessao(db, cliente.id, AGORA) is None


def test_proxima_sessao_ignora_pacientes_de_outro_cliente(db, fono, cliente, paciente):
    outro = criar_cliente(db, email="outro@exemplo.com", nome="Outra Pessoa")
    paciente_alheio = criar_paciente(db, outro.id, nome="Paciente Alheio")
    criar_plano(db, paciente_alheio, fono, [(datetime(2026, 3, 11, 9, 0), AGENDADA)])
    criar_plano(db, paciente, fono, [(datetime(2026, 3, 20, 9, 0), AGENDADA)])

    assert inicio_service.proxima_sessao(db, cliente.id, AGORA).paciente == "Pedro Souza"
    assert inicio_service.proxima_sessao(db, outro.id, AGORA).paciente == "Paciente Alheio"


# --- Planos: situação e progresso ----------------------------------------------------------------

def test_situacao_plano_por_status():
    def situacao(status_plano, criado_em=CRIADO_NO_PRAZO, **sessoes):
        return inicio_service.situacao_plano(status_plano, Counter(sessoes), criado_em, AGORA)

    pendente = {PENDENTE: 4}
    assert situacao(StatusPlano.ATIVO, **{AGENDADA: 3, REALIZADA: 1}) == ATIVO
    assert situacao(StatusPlano.ATIVO, **pendente) == AGUARDANDO_PAGAMENTO
    assert situacao(StatusPlano.ATIVO, CRIADO_VENCIDO, **pendente) is None
    assert situacao(StatusPlano.ENCERRADO, **{REALIZADA: 4}) == ENCERRADO
    assert situacao(StatusPlano.ENCERRADO, CRIADO_VENCIDO, **pendente) == ENCERRADO


def test_prazo_de_pagamento_no_limite_ainda_aguarda():
    criado_em_utc = datetime(2026, 3, 10, 11, 0)  # 08:00 local + 4h = 12:00
    no_limite = datetime(2026, 3, 10, 12, 0)
    status = Counter({PENDENTE: 1})
    assert inicio_service.situacao_plano(StatusPlano.ATIVO, status, criado_em_utc, no_limite) == AGUARDANDO_PAGAMENTO
    assert inicio_service.situacao_plano(StatusPlano.ATIVO, status, criado_em_utc, no_limite + timedelta(seconds=1)) is None


def test_planos_do_cliente_progresso_e_situacao(db, fono, cliente, paciente):
    ativo = criar_plano(db, paciente, fono, [
        (datetime(2026, 3, 3, 14, 0), REALIZADA),
        (datetime(2026, 3, 5, 14, 0), REALIZADA),
        (datetime(2026, 3, 12, 14, 0), AGENDADA),
        (datetime(2026, 3, 17, 14, 0), AGENDADA),
    ])
    aguardando = criar_plano(db, paciente, fono, [(datetime(2026, 3, 19, 9, 0), PENDENTE)], criado_em=CRIADO_NO_PRAZO)
    encerrado = criar_plano(db, paciente, fono, [(datetime(2026, 2, 3, 9, 0), REALIZADA)], status_plano=StatusPlano.ENCERRADO)

    resumos = {r.plano.id: r for r in inicio_service.planos_do_cliente(db, cliente.id, AGORA)}

    assert (resumos[ativo.id].situacao, resumos[ativo.id].realizadas, resumos[ativo.id].total_sessoes) == (ATIVO, 2, 4)
    assert resumos[aguardando.id].situacao == AGUARDANDO_PAGAMENTO
    assert resumos[aguardando.id].tempo_restante == timedelta(hours=3)
    assert resumos[encerrado.id].situacao == ENCERRADO
    assert resumos[ativo.id].tempo_restante is None


def test_planos_do_cliente_ordena_aguardando_ativo_encerrado(db, fono, cliente, paciente):
    encerrado = criar_plano(db, paciente, fono, [(datetime(2026, 2, 3, 9, 0), REALIZADA)], status_plano=StatusPlano.ENCERRADO)
    ativo = criar_plano(db, paciente, fono, [(datetime(2026, 3, 12, 9, 0), AGENDADA)])
    aguardando = criar_plano(db, paciente, fono, [(datetime(2026, 3, 19, 9, 0), PENDENTE)], criado_em=CRIADO_NO_PRAZO)

    ids = [r.plano.id for r in inicio_service.planos_do_cliente(db, cliente.id, AGORA)]
    assert ids == [aguardando.id, ativo.id, encerrado.id]


def test_plano_com_pagamento_vencido_nao_aparece(db, fono, cliente, paciente):
    criar_plano(db, paciente, fono, [(datetime(2026, 3, 19, 9, 0), PENDENTE)], criado_em=CRIADO_VENCIDO)
    assert inicio_service.planos_do_cliente(db, cliente.id, AGORA) == []


def test_planos_do_cliente_ignora_pacientes_de_outro_cliente(db, fono, cliente, paciente):
    outro = criar_cliente(db, email="outro@exemplo.com", nome="Outra Pessoa")
    criar_plano(db, criar_paciente(db, outro.id, nome="Paciente Alheio"), fono, [(datetime(2026, 3, 12, 9, 0), AGENDADA)])
    meu = criar_plano(db, paciente, fono, [(datetime(2026, 3, 13, 9, 0), AGENDADA)])

    assert [r.plano.id for r in inicio_service.planos_do_cliente(db, cliente.id, AGORA)] == [meu.id]


# --- Telas ---------------------------------------------------------------------------------------

def test_inicio_do_cliente_vazio(client, db, cliente):
    logar(client, cliente.email)
    resposta = client.get("/")
    assert resposta.status_code == 200
    html = _html(resposta)
    assert "Bom dia, Maria" in html
    assert "Nenhuma sessão agendada" in html
    assert "Nenhum plano contratado" in html
    assert html.count('href="/especialidades" class="btn btn-primary"') == 2
    assert 'href="/admin/funcionarios"' not in html


def test_inicio_do_cliente_com_dados(client, db, fono, cliente, paciente):
    criar_plano(db, paciente, fono, [
        (datetime(2026, 3, 3, 14, 0), REALIZADA),
        (datetime(2026, 3, 11, 14, 0), AGENDADA),
        (datetime(2026, 3, 18, 14, 0), AGENDADA),
    ], valor_total="450.00")
    criar_plano(db, paciente, fono, [(datetime(2026, 3, 20, 9, 0), PENDENTE)], criado_em=CRIADO_NO_PRAZO)
    criar_plano(db, paciente, fono, [(datetime(2026, 2, 6, 9, 0), REALIZADA)], status_plano=StatusPlano.ENCERRADO)
    criar_plano(db, paciente, fono, [(datetime(2026, 3, 23, 8, 0), PENDENTE)], criado_em=CRIADO_VENCIDO)
    logar(client, cliente.email)

    html = _html(client.get("/"))

    assert "Amanhã, às 14:00" in html
    assert "1 de 3 sessões realizadas" in html
    assert "R$ 450,00" in html
    assert "Terça, Quarta, às 14:00" in html
    assert "03/03/2026 a 18/03/2026" in html
    assert html.count(">Ativo</span>") == 1
    assert html.count(">Aguardando pagamento</span>") == 1
    assert html.count(">Encerrado</span>") == 1
    assert "restam 3h" in html
    assert "Segunda, às 08:00" not in html  # plano com pagamento vencido (RN09)


def test_inicio_do_cliente_mostra_data_quando_nao_e_hoje_nem_amanha(client, db, fono, cliente, paciente):
    criar_plano(db, paciente, fono, [(datetime(2026, 3, 13, 15, 0), AGENDADA)])
    logar(client, cliente.email)
    assert "Sexta, 13/03/2026, às 15:00" in _html(client.get("/"))


def test_inicio_do_cliente_nao_mostra_dados_de_outro_cliente(client, db, fono, cliente):
    outro = criar_cliente(db, email="outro@exemplo.com", nome="Outra Pessoa")
    criar_plano(db, criar_paciente(db, outro.id, nome="Paciente Alheio"), fono, [(datetime(2026, 3, 11, 9, 0), AGENDADA)])
    logar(client, cliente.email)

    html = _html(client.get("/"))
    assert "Paciente Alheio" not in html
    assert "Nenhuma sessão agendada" in html
    assert "Nenhum plano contratado" in html


def test_inicio_do_cliente_nao_adianta_funcionalidades_futuras(client, db, fono, cliente, paciente):
    criar_plano(db, paciente, fono, [(datetime(2026, 3, 11, 14, 0), AGENDADA)])
    criar_plano(db, paciente, fono, [(datetime(2026, 3, 20, 9, 0), PENDENTE)], criado_em=CRIADO_NO_PRAZO)
    criar_plano(db, paciente, fono, [(datetime(2026, 2, 6, 9, 0), REALIZADA)], status_plano=StatusPlano.ENCERRADO)
    logar(client, cliente.email)

    html = _html(client.get("/")).lower()
    for texto in ["remarcar", "histórico", "pagar agora", "renovar", "minha agenda"]:
        assert texto not in html, texto


def test_inicio_do_funcionario(client, db, fono):
    logar(client, fono.usuario.email)
    html = _html(client.get("/"))
    assert "Bom dia, Carlos" in html
    assert "Fonoaudiologia" in html
    assert 'href="/conta/alterar-senha" class="cartao cartao-atalho"' in html
    assert "Próxima sessão" not in html and "Planos de atendimento" not in html
    assert "Funcionários" not in html


def test_inicio_do_admin_com_contagens(client, db, fono):
    psico = criar_especialidade(db, "Psicologia")
    criar_funcionario(db, psico, email="psi@exemplo.com", nome="Paula Psi")
    criar_especialidade(db, "Psicopedagogia")
    admin = criar_admin(db)
    funcionario_service.desativar(db, fono.usuario.id, AGORA)
    logar(client, admin.email)

    html = _html(client.get("/"))
    assert "Bom dia, Ana" in html
    assert re.search(r"Funcionários</span> <span[^>]*>1</span> <span[^>]*>funcionário ativo<", html)
    assert re.search(r"Especialidades</span> <span[^>]*>3</span> <span[^>]*>especialidades cadastradas<", html)
    assert "Próxima sessão" not in html and "Planos de atendimento" not in html


def test_inicio_do_admin_sem_cadastros(client, db):
    logar(client, criar_admin(db).email)
    html = _html(client.get("/"))
    assert re.search(r">0</span> <span[^>]*>funcionários ativos<", html)
    assert re.search(r">0</span> <span[^>]*>especialidades cadastradas<", html)


@pytest.mark.parametrize("hora, esperado", [(4, "Boa noite"), (13, "Boa tarde"), (19, "Boa noite")])
def test_inicio_usa_o_horario_local_na_saudacao(client, db, cliente, monkeypatch, hora, esperado):
    monkeypatch.setattr(inicio_router, "agora_local", lambda: AGORA.replace(hour=hora))
    logar(client, cliente.email)
    assert f"{esperado}, Maria" in _html(client.get("/"))


def test_visitante_ve_a_landing(client):
    html = _html(client.get("/"))
    assert "Pronto para começar?" in html
    assert "Bom dia" not in html
