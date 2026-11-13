import re
from datetime import datetime, timezone

import pytest
from sqlalchemy import event

from app.models.cliente import Cliente
from app.models.pagamento import FormaPagamento, Pagamento, StatusPagamento
from app.models.sessao import StatusSessao
from app.routers import cliente as cliente_router
from app.services.pagamento_service import MSG_CPF
from tests.abacatepay_falsa import cobranca_pix
from tests.fabricas import criar_cliente, criar_especialidade, criar_funcionario, criar_paciente, criar_plano, logar

PENDENTE = StatusSessao.PENDENTE_PAGAMENTO
AGENDADA = StatusSessao.AGENDADA
REALIZADA = StatusSessao.REALIZADA
AGORA = datetime(2026, 8, 3, 10, 20)  # segunda, horário local
CRIADO_10H = datetime(2026, 8, 3, 13, 0)  # UTC = 10:00 local: o Pix vence às 14:00
CPF = "529.982.247-25"


@pytest.fixture(autouse=True)
def relogio_fixo(monkeypatch):
    monkeypatch.setattr(cliente_router, "agora_local", lambda: AGORA)
    monkeypatch.setattr(cliente_router, "agora_utc", lambda: datetime(2026, 8, 3, 13, 20, tzinfo=timezone.utc))


@pytest.fixture
def carla(db):
    return criar_funcionario(db, criar_especialidade(db, "Psicopedagogia", "150.00"), email="carla@exemplo.com", nome="Carla Mendes")


@pytest.fixture
def maria(client, db):
    cliente = criar_cliente(db, email="maria@exemplo.com", nome="Maria Souza")
    logar(client, cliente.email)
    return cliente


@pytest.fixture
def pedro(db, maria):
    return criar_paciente(db, maria.id, nome="Pedro Souza")


def _reservado(db, pedro, carla, primeira=datetime(2026, 8, 14, 14, 0)):
    return criar_plano(db, pedro, carla, [(primeira, PENDENTE), (datetime(2026, 8, 21, 14, 0), PENDENTE)], criado_em=CRIADO_10H)


def _html(resposta) -> str:
    assert resposta.status_code == 200, resposta.status_code
    return " ".join(resposta.text.split())


def _cabecalho(html: str) -> str:
    return re.search(r'<div class="plano-cab [\w-]+">.*?</div>', html).group(0)


# --- Plano reservado ---------------------------------------------------------------------------------------

def test_plano_reservado_com_faixa_restam_e_pagar_agora(client, db, carla, pedro):
    plano = _reservado(db, pedro, carla)
    html = _html(client.get(f"/planos/{plano.id}"))
    faixa = re.search(r'<section class="plano-urgente".*?</section>', html).group(0)
    assert '<p class="plano-urgente-titulo" id="titulo-prazo">Pague até <strong>hoje, às 14:00</strong></p>' in faixa
    assert "Restam 3h 40min." in faixa
    assert f'<a href="/planos/{plano.id}/pagamento" class="btn btn-primary">' in faixa and "Pagar agora" in faixa
    assert html.index('class="plano-urgente"') < html.index('class="lista-dados plano-dados"')
    assert '<details class="rev-datas"> <summary>Ver as datas</summary>' in html  # fechado
    assert html.count("Pagar agora") == 1


def test_plano_com_prazo_vencido_mantem_o_comportamento(client, db, carla, pedro, monkeypatch):
    plano = _reservado(db, pedro, carla)
    monkeypatch.setattr(cliente_router, "agora_local", lambda: datetime(2026, 8, 3, 14, 30))  # 30 min depois do prazo
    html = _html(client.get(f"/planos/{plano.id}"))
    assert "plano-urgente" not in html and "Restam" not in html and "Pagar agora" not in html
    assert "O pagamento não foi feito até 03/08/2026 às 14:00." in html
    assert ">Prazo de pagamento encerrado</span>" in _cabecalho(html)


# --- Forma de pagamento ------------------------------------------------------------------------------------

def test_forma_de_pagamento_com_pix_e_boleto(client, db, carla, pedro):
    plano = _reservado(db, pedro, carla)  # 1ª sessão sexta 14/08: boleto em 3 dias úteis, 06/08
    html = _html(client.get(f"/planos/{plano.id}/pagamento"))
    assert "Como você quer pagar?</h1>" in html
    pix = re.search(r'<label class="opcao-pagamento" for="forma-pix">.*?</label>', html).group(0)
    assert 'Pague em até 4 horas, até <strong class="pag-prazo">hoje, às 14:00</strong>' in pix
    boleto = re.search(r'<label class="opcao-pagamento" for="forma-boleto">.*?</label>', html).group(0)
    assert "Vence em 06/08/2026. O horário fica reservado até o vencimento." in boleto
    assert 'name="forma" id="forma-pix" value="pix"' in html and 'name="forma" id="forma-boleto" value="boleto"' in html
    assert ">Gerar cobrança</button>" in html and "boleto-indisponivel" not in html
    valor = re.search(r'<aside class="cartao resumo-plano".*?</aside>', html).group(0)
    assert "R$ 600,00" in valor and "Escolha a forma de pagamento até <strong>hoje, às 14:00</strong>" in valor


def test_forma_de_pagamento_sem_boleto(client, db, carla, pedro):
    plano = _reservado(db, pedro, carla, primeira=datetime(2026, 8, 5, 14, 0))  # quarta 05/08: sem tempo para o boleto
    html = _html(client.get(f"/planos/{plano.id}/pagamento"))
    assert 'value="boleto"' not in html
    assert "O boleto não está disponível para este plano porque a primeira sessão é muito próxima." in html


def test_cpf_continua_exigido_para_gerar_a_cobranca(client, db, carla, pedro, api_falsa):
    plano = _reservado(db, pedro, carla)
    resposta = client.post(f"/planos/{plano.id}/pagamento", data={"forma": "pix"}, follow_redirects=False)
    assert resposta.status_code == 503 and MSG_CPF in resposta.text
    assert db.query(Pagamento).filter_by(status=StatusPagamento.CONFIRMADO).count() == 0


# --- Componentes ---------------------------------------------------------------------------------------------

def test_dados_do_plano_vem_do_mesmo_componente(client, db, carla, pedro):
    plano = _reservado(db, pedro, carla)
    blocos = [re.search(r'<dl class="lista-dados plano-dados">.*?</dl>', _html(client.get(url))).group(0)
              for url in (f"/planos/{plano.id}", f"/planos/{plano.id}/pagamento")]
    assert blocos[0] == blocos[1]
    assert re.findall(r"<dt>([^<]+)</dt>", blocos[0]) == [
        "Pessoa atendida", "Especialidade", "Profissional", "Dias e horário", "Frequência e duração", "Período",
    ]
    assert "<dd>14/08/2026 a 21/08/2026 · 2 sessões</dd>" in blocos[0]


@pytest.mark.parametrize("situacao, sessoes, selo", [
    ("aguardando", [(datetime(2026, 8, 14, 14, 0), PENDENTE)], '<span class="badge rounded-pill status-pendente">Aguardando pagamento</span>'),
    ("ativo", [(datetime(2026, 8, 14, 14, 0), AGENDADA)], '<span class="badge rounded-pill status-ativo">Ativo</span>'),
    ("encerrado", [(datetime(2026, 7, 10, 14, 0), REALIZADA)], '<span class="badge rounded-pill status-inativo">Encerrado</span>'),
])
def test_cabecalho_com_os_tres_selos_na_pagina_do_plano(client, db, carla, pedro, situacao, sessoes, selo):
    plano = criar_plano(db, pedro, carla, sessoes, criado_em=CRIADO_10H)
    cab = _cabecalho(_html(client.get(f"/planos/{plano.id}")))
    assert "<strong>Pedro Souza</strong>" in cab and '<span class="chip-especialidade">Psicopedagogia</span>' in cab
    assert "<span>com Carla Mendes</span>" in cab and selo in cab
    assert 'class="plano-cab esp-psicopedagogia"' in cab


def test_cabecalho_na_forma_de_pagamento_e_na_cobranca(client, db, carla, pedro, maria, api_falsa):
    db.get(Cliente, maria.id).cpf = CPF
    db.commit()
    plano = _reservado(db, pedro, carla)
    api_falsa.responder("/transparents/create", cobranca_pix(valor=60000))
    api_falsa.responder("/transparents/list", [cobranca_pix(valor=60000)])
    assert "status-pendente\">Aguardando pagamento" in _cabecalho(_html(client.get(f"/planos/{plano.id}/pagamento")))
    assert client.post(f"/planos/{plano.id}/pagamento", data={"forma": "pix"}, follow_redirects=False).status_code == 303
    cobranca = _html(client.get(f"/planos/{plano.id}/cobranca"))
    assert "status-pendente\">Aguardando pagamento" in _cabecalho(cobranca) and "Pedro Souza" in _cabecalho(cobranca)


def test_cabecalho_na_cobranca_de_plano_pago(client, db, carla, pedro, api_falsa):
    plano = criar_plano(db, pedro, carla, [(datetime(2026, 8, 14, 14, 0), AGENDADA)], criado_em=CRIADO_10H)
    db.add(Pagamento(plano_id=plano.id, forma_pagamento=FormaPagamento.PIX, status=StatusPagamento.CONFIRMADO,
                     confirmado_em=datetime(2026, 8, 3, 13, 10)))
    db.commit()
    html = _html(client.get(f"/planos/{plano.id}/cobranca"))
    assert 'status-ativo">Ativo</span>' in _cabecalho(html) and "Pagamento confirmado" in html


def test_telas_do_plano_sem_javascript(client, db, carla, pedro):
    plano = _reservado(db, pedro, carla)
    for url in (f"/planos/{plano.id}", f"/planos/{plano.id}/pagamento"):
        assert _html(client.get(url)).count("<script") == 1  # só o bundle do Bootstrap


# --- SQL -----------------------------------------------------------------------------------------------------

def _contar_sql(client, db, url) -> int:
    comandos = []
    ouvinte = lambda *args: comandos.append(args[2])  # noqa: E731
    event.listen(db.get_bind(), "before_cursor_execute", ouvinte)
    try:
        assert client.get(url).status_code == 200
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", ouvinte)
    return len(comandos)


def test_numero_de_consultas_igual_ao_de_antes(client, db, carla, pedro, maria):
    criar_paciente(db, maria.id, nome="Ana Souza")
    plano = _reservado(db, pedro, carla)
    medidas = []
    for url in (f"/contratar/{carla.usuario.id}", f"/planos/{plano.id}", f"/planos/{plano.id}/pagamento"):
        _contar_sql(client, db, url)  # aquece: a 1ª requisição recarrega o usuário logado
        medidas.append(_contar_sql(client, db, url))
    assert medidas == [3, 4, 6]  # medidos no código anterior a este bloco: passo 2, plano e forma de pagamento
