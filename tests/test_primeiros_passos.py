import re
from datetime import date, datetime, timezone

import pytest

from app.models.cliente import Cliente
from app.models.sessao import StatusSessao
from app.routers import cliente as cliente_router
from app.routers import inicio as inicio_router
from app.services import contratacao_service, inicio_service
from tests.fabricas import criar_cliente, criar_especialidade, criar_funcionario, criar_paciente, criar_plano, logar

AGORA = datetime(2026, 8, 1, 10, 0)  # sábado, horário local
AGORA_UTC = datetime(2026, 8, 1, 13, 0, tzinfo=timezone.utc)
CRIADO_EM_UTC = datetime(2026, 8, 1, 12, 0)  # 09:00 local: o Pix vence às 13:00 local
CPF_VALIDO = "529.982.247-25"
PENDENTE = StatusSessao.PENDENTE_PAGAMENTO
FORM_CADASTRO = {"nome": "João Lima", "email": "joao@exemplo.com", "telefone": "54 98888-0000", "senha": "senha123"}


@pytest.fixture(autouse=True)
def relogio_fixo(monkeypatch):
    for modulo in (cliente_router, inicio_router):
        monkeypatch.setattr(modulo, "agora_local", lambda: AGORA)
    monkeypatch.setattr(cliente_router, "agora_utc", lambda: AGORA_UTC)


@pytest.fixture
def carla(db):
    return criar_funcionario(db, criar_especialidade(db, "Fonoaudiologia", "150.00"), email="carla@exemplo.com", nome="Carla Fono")


@pytest.fixture
def maria(db):
    return criar_cliente(db, email="maria@exemplo.com", nome="Maria Souza")


@pytest.fixture
def maria_logada(client, maria):
    logar(client, maria.email)
    return maria


def _html(resposta) -> str:
    return " ".join(resposta.text.split())


def _passos(html: str) -> str:
    return re.search(r'<section class="cartao primeiros-passos.*?</section>', html, re.S).group(0)


def _passo_atual(html: str) -> str:
    return re.search(r'<li class="passo passo-atual" aria-current="step">.*?</li>', _passos(html), re.S).group(0)


# --- Regra dos passos (sem banco) -----------------------------------------------------------------

def _resumo(situacao, plano_id=1):
    plano = type("PlanoFalso", (), {"id": plano_id})()
    return inicio_service.ResumoPlano(plano, "Pedro", "Fono", "Carla", situacao, 4, 0, None)


@pytest.mark.parametrize("tem_atendido, planos, concluidos, proximo", [
    (False, [], (False, False, False, False), 1),
    (True, [], (True, False, False, False), 2),
    (True, [_resumo(inicio_service.AGUARDANDO_PAGAMENTO, 7)], (True, True, False, False), 3),
])
def test_passos_marcam_o_que_ja_foi_feito(tem_atendido, planos, concluidos, proximo):
    passos = inicio_service.primeiros_passos(tem_atendido, planos)
    assert (passos.concluidos, passos.proximo) == (concluidos, proximo)


def test_plano_aguardando_pagamento_leva_ao_pagamento_dele():
    passos = inicio_service.primeiros_passos(True, [_resumo(inicio_service.AGUARDANDO_PAGAMENTO, 7)])
    assert passos.plano_aguardando_id == 7


@pytest.mark.parametrize("situacao", [inicio_service.ATIVO, inicio_service.ENCERRADO])
def test_passos_somem_com_o_primeiro_plano_pago(situacao):
    planos = [_resumo(inicio_service.AGUARDANDO_PAGAMENTO, 7), _resumo(situacao, 8)]
    assert inicio_service.primeiros_passos(True, planos) is None


# --- Home do Cliente --------------------------------------------------------------------------------

def test_cliente_novo_ve_os_passos_com_cadastrar_atendido_em_destaque(client, maria_logada):
    html = _html(client.get("/"))
    passos = _passos(html)
    titulos = re.findall(r'<p class="passo-titulo">([^<]+)', passos)
    assert titulos == ["Cadastrar o atendido", "Contratar um plano", "Pagar", "Acompanhar as sessões"]
    atual = _passo_atual(html)
    assert "Cadastrar o atendido" in atual and 'href="/pacientes/novo"' in atual
    assert passos.count('class="btn btn-primary') == 1
    assert "passo-concluido" not in passos


def test_com_atendido_o_proximo_passo_e_contratar(client, db, maria_logada):
    criar_paciente(db, maria_logada.id)
    html = _html(client.get("/"))
    assert 'class="passo passo-concluido"' in _passos(html) and "(concluído)" in _passos(html)
    atual = _passo_atual(html)
    assert "Contratar um plano" in atual and 'href="/especialidades"' in atual


def test_com_plano_aguardando_o_proximo_passo_e_pagar(client, db, carla, maria_logada):
    pedro = criar_paciente(db, maria_logada.id)
    plano = criar_plano(db, pedro, carla, [(datetime(2026, 8, 10, 14, 0), PENDENTE)], criado_em=CRIADO_EM_UTC)
    html = _html(client.get("/"))
    assert _passos(html).count('class="passo passo-concluido"') == 2
    atual = _passo_atual(html)
    assert "Pagar" in atual and f'href="/planos/{plano.id}/pagamento"' in atual


def test_com_plano_pago_a_home_volta_ao_normal(client, db, carla, maria_logada):
    pedro = criar_paciente(db, maria_logada.id)
    criar_plano(db, pedro, carla, [(datetime(2026, 8, 10, 14, 0), StatusSessao.AGENDADA)], criado_em=CRIADO_EM_UTC)
    html = _html(client.get("/"))
    assert "Primeiros passos" not in html and "Próxima sessão" in html


def test_cadastro_leva_ao_login_e_o_primeiro_login_mostra_os_primeiros_passos(client, db):
    # HU17: cadastro válido redireciona para o login, sem abrir sessão
    cadastro = client.post("/cadastro", data=FORM_CADASTRO, follow_redirects=False)
    assert cadastro.headers["location"] == "/login?cadastro=ok" and not cadastro.cookies
    assert "Cadastro realizado! Faça login para ver os primeiros passos." in _html(client.get(cadastro.headers["location"]))
    assert "Primeiros passos" not in _html(client.get("/"))

    html = _html(client.post("/login", data={"email": FORM_CADASTRO["email"], "senha": FORM_CADASTRO["senha"]}))
    assert "Bom dia, João" in html and "Primeiros passos" in html
    assert 'href="/pacientes/novo"' in _passo_atual(html)
    assert "Conta criada" not in html


# --- Telas da contratação ----------------------------------------------------------------------------

def _etapa_atual(html: str) -> str:
    return re.search(r'<li class="etapa etapa-atual" aria-current="step">.*?</li>', html, re.S).group(0)


def test_especialidades_mostram_etapa_1_explicacao_e_aviso_sem_atendido(client, carla, maria_logada):
    html = _html(client.get("/especialidades"))
    assert "Especialidade e profissional" in _etapa_atual(html)
    assert html.index('aria-label="Etapas da contratação"') < html.index("Fonoaudiologia")
    assert "O que é um plano?" in html and "sessões são criadas automaticamente" in html
    assert "Antes de contratar, cadastre a pessoa que será atendida." in html and 'href="/pacientes/novo"' in html


def test_profissionais_mostram_etapa_1_e_aviso_some_com_atendido(client, db, carla, maria_logada):
    criar_paciente(db, maria_logada.id)
    html = _html(client.get(f"/especialidades/{carla.especialidade.id}/profissionais"))
    assert "Especialidade e profissional" in _etapa_atual(html)
    assert "Antes de contratar" not in html
    assert "Antes de contratar" not in _html(client.get("/especialidades"))


def test_formulario_mostra_etapa_2_e_explicacao_acima_do_formulario(client, db, carla, maria_logada):
    criar_paciente(db, maria_logada.id)
    html = _html(client.get(f"/contratar/{carla.usuario.id}"))
    assert "Dias e horários" in _etapa_atual(html)
    assert re.search(r'<li class="etapa etapa-concluida">.*?Especialidade e profissional.*?\(concluída\)', html)
    assert html.index("O que é um plano?") < html.index('id="form-contratacao"')


def test_contratacao_sem_atendido_mantem_aviso_com_link(client, carla, maria_logada):
    html = _html(client.get(f"/contratar/{carla.usuario.id}"))
    assert "Dias e horários" in _etapa_atual(html)
    assert "Cadastre um atendido para contratar" in html and 'href="/pacientes/novo"' in html


def test_resumo_mostra_etapa_3_sessoes_e_prazo_da_rn09(client, db, carla, maria_logada):
    db.get(Cliente, maria_logada.id).cpf = CPF_VALIDO
    db.commit()
    pedro = criar_paciente(db, maria_logada.id)
    dados = {"paciente_id": str(pedro.id), "celula": ["0-14"], "frequencia_semanal": "1", "duracao": "mensal", "observacao": ""}
    html = _html(client.post(f"/contratar/{carla.usuario.id}", data=dados))

    assert "Revisão e pagamento" in _etapa_atual(html)
    for trecho in (
        "Fonoaudiologia", "Carla Fono", "Pessoa atendida</dt> <dd>Pedro Souza", "Segunda, às 14:00",
        "Quantidade de sessões</dt> <dd>5", "Primeira sessão</dt> <dd>03/08/2026 às 14:00",
        "Última sessão</dt> <dd>31/08/2026 às 14:00", "R$ 750,00",
        "Depois de confirmar, é preciso pagar dentro do prazo.",
        "Pix: até 01/08/2026 às 14:00 (4 horas).",
    ):
        assert trecho in html, trecho
    # Primeira sessão na segunda 03/08: o 2º dia útil antes dela já passou (RN09)
    assert "Boleto: indisponível para este plano" in html


def test_prazo_previsto_segue_a_rn09():
    prazo = contratacao_service.prazo_previsto(CRIADO_EM_UTC, date(2026, 8, 10), AGORA)
    assert prazo.pix == datetime(2026, 8, 1, 13, 0)
    assert prazo.vencimento_boleto == date(2026, 8, 5)

    sem_boleto = contratacao_service.prazo_previsto(CRIADO_EM_UTC, date(2026, 8, 3), AGORA)
    assert sem_boleto.vencimento_boleto is None


# --- Landing page --------------------------------------------------------------------------------

def test_como_funciona_tem_os_mesmos_passos_na_mesma_ordem(client):
    html = _html(client.get("/"))
    secao = html[html.index("Como funciona"):html.index("Pronto para começar?")]
    titulos = re.findall(r'<h3 class="titulo-serifa h5">([^<]+)</h3>', secao)
    assert titulos == ["Cadastre o atendido", "Contrate um plano", "Pague", "Acompanhe as sessões"]
