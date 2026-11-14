import re
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from app.models.cliente import Cliente
from app.models.pagamento import FormaPagamento
from app.models.sessao import StatusSessao
from app.routers import admin as admin_router
from app.routers import cliente as cliente_router
from app.routers import inicio as inicio_router
from app.services import admin_clientes_service, contratacao_service, inicio_cliente_service
from app.services.contratacao_service import TextoPrazo, texto_prazo_pagamento
from tests.abacatepay_falsa import cobranca_boleto, cobranca_pix
from tests.fabricas import criar_admin, criar_cliente, criar_especialidade, criar_funcionario, criar_paciente, criar_plano, logar

PENDENTE = StatusSessao.PENDENTE_PAGAMENTO
REALIZADA = StatusSessao.REALIZADA
AGORA = datetime(2026, 8, 3, 10, 20)  # segunda, horário local
CRIADO_10H = datetime(2026, 8, 3, 13, 0)  # UTC = 10:00 local: o Pix vence às 14:00
COM_BOLETO = date(2026, 8, 14)  # boleto em 3 dias úteis: 06/08
SEM_BOLETO = date(2026, 8, 5)  # 2º dia útil antes de 05/08 já passou
CPF = "529.982.247-25"
TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates"


@pytest.fixture(autouse=True)
def relogio_fixo(monkeypatch):
    for modulo in (cliente_router, inicio_router, admin_router):
        monkeypatch.setattr(modulo, "agora_local", lambda: AGORA)
    monkeypatch.setattr(cliente_router, "agora_utc", lambda: datetime(2026, 8, 3, 13, 20, tzinfo=timezone.utc))


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


def _reservado(db, pedro, carla, primeira=COM_BOLETO):
    return criar_plano(db, pedro, carla, [(datetime.combine(primeira, datetime.min.time()).replace(hour=14), PENDENTE)],
                       criado_em=CRIADO_10H)


def _encerrado(db, pedro, carla):
    return criar_plano(db, pedro, carla, [(datetime(2026, 7, d, 14, 0), REALIZADA) for d in (6, 13, 20, 27)])


def _html(resposta) -> str:
    assert resposta.status_code == 200, resposta.status_code
    return " ".join(resposta.text.split())


def _revisao(client, carla, pedro) -> str:
    dados = {"paciente_id": str(pedro.id), "celula": ["0-14"], "frequencia_semanal": "1", "duracao": "mensal", "observacao": ""}
    return _html(client.post(f"/contratar/{carla.usuario.id}", data=dados))


# --- A função nos três estados (RNF14: `agora` fixo) -----------------------------------------------------

def test_sem_cobranca_hoje_com_boleto_oferecido():
    texto = texto_prazo_pagamento(CRIADO_10H, COM_BOLETO, None, AGORA)
    assert (texto.acao, texto.quando, texto.restam, texto.sem_cobranca) == ("Gere a cobrança até", "hoje, às 14:00", "3h 40min", True)
    assert texto.titulo == "Gere a cobrança até hoje, às 14:00"
    assert texto.linha == "Gere a cobrança até hoje, às 14:00 (restam 3h 40min)"
    assert texto.vencimento_boleto == date(2026, 8, 6) and texto.vencimento == "Vence em 06/08/2026"
    assert texto.neutro == "Reserva até hoje, às 14:00 (sem cobrança gerada)"
    assert texto.prazo == datetime(2026, 8, 3, 14, 0)


def test_sem_cobranca_sem_boleto_oferecido():
    texto = texto_prazo_pagamento(CRIADO_10H, SEM_BOLETO, None, AGORA)
    assert texto.vencimento_boleto is None and texto.vencimento is None
    assert texto.linha == "Gere a cobrança até hoje, às 14:00 (restam 3h 40min)"


def test_sem_cobranca_amanha():
    criado = datetime(2026, 8, 4, 1, 0)  # UTC = 22:00 local de 03/08: o Pix vence às 02:00 de 04/08
    texto = texto_prazo_pagamento(criado, COM_BOLETO, None, datetime(2026, 8, 3, 22, 30))
    assert texto.linha == "Gere a cobrança até amanhã, às 02:00 (restam 3h 30min)"
    assert texto.neutro == "Reserva até amanhã, às 02:00 (sem cobrança gerada)"


def test_cobranca_pix():
    texto = texto_prazo_pagamento(CRIADO_10H, COM_BOLETO, FormaPagamento.PIX, AGORA)
    assert texto.linha == "Pague até hoje, às 14:00 (restam 3h 40min)" and not texto.sem_cobranca
    assert texto.vencimento is None and texto.neutro == "Pix até hoje, às 14:00"


def test_cobranca_boleto_com_data():
    texto = texto_prazo_pagamento(CRIADO_10H, COM_BOLETO, FormaPagamento.BOLETO, AGORA)
    assert texto.linha == "Pague até 06/08, às 23:59"  # mais de 24h: sem "restam"
    assert texto.vencimento == "Vence em 06/08/2026" and texto.neutro == "Boleto vence em 06/08/2026"


def test_cobranca_boleto_no_ultimo_dia_mostra_restam():
    texto = texto_prazo_pagamento(CRIADO_10H, COM_BOLETO, FormaPagamento.BOLETO, datetime(2026, 8, 6, 20, 0))
    assert texto.linha == "Pague até hoje, às 23:59 (restam 3h 59min)"


@pytest.mark.parametrize("forma, agora", [
    (None, datetime(2026, 8, 3, 14, 0)),  # exatamente no prazo
    (None, datetime(2026, 8, 3, 15, 30)),
    (FormaPagamento.PIX, datetime(2026, 8, 3, 18, 0)),
    (FormaPagamento.BOLETO, datetime(2026, 8, 7, 9, 0)),
])
def test_prazo_vencido_nunca_mostra_contagem_negativa(forma, agora):
    texto = texto_prazo_pagamento(CRIADO_10H, COM_BOLETO, forma, agora)
    assert texto.restam is None and "restam" not in texto.linha and "-" not in texto.linha


# --- Templates e constante ---------------------------------------------------------------------------------

def test_nenhum_template_escreve_o_numero_de_horas():
    for caminho in TEMPLATES.rglob("*.html"):
        texto = caminho.read_text(encoding="utf-8")
        assert not re.search(r"\b\d+\s+horas\b", texto), caminho.name
        assert "Pague em até" not in texto and "Escolha a forma de pagamento até" not in texto, caminho.name


def test_horas_da_revisao_e_da_renovacao_vem_da_constante(client, db, monkeypatch, carla, pedro):
    monkeypatch.setattr(cliente_router, "PRAZO_PAGAMENTO_HORAS", 6)
    revisao = _revisao(client, carla, pedro)
    assert "Você tem 6 horas para gerar a cobrança; sem ela, os horários voltam a ficar livres para outros atendidos." in revisao
    assert "4 horas" not in revisao
    renovacao = _html(client.get(f"/planos/{_encerrado(db, pedro, carla).id}/renovar"))
    assert "você terá 6 horas para gerar a cobrança" in renovacao and "4 horas" not in renovacao


# --- Telas -------------------------------------------------------------------------------------------------

def test_faixa_sem_cobranca_com_boleto(client, db, carla, pedro):
    plano = _reservado(db, pedro, carla)
    faixa = re.search(r'<section class="plano-urgente".*?</section>', _html(client.get(f"/planos/{plano.id}"))).group(0)
    assert '<p class="plano-urgente-titulo" id="titulo-prazo">Gere a cobrança até <strong>hoje, às 14:00</strong></p>' in faixa
    assert ("Restam 3h 40min. Com Pix, o pagamento também vale até esse horário; com boleto, a reserva se estende até o "
            "vencimento (06/08/2026). Sem cobrança, os horários voltam a ficar livres e o plano expira.") in faixa
    assert f'<a href="/planos/{plano.id}/pagamento" class="btn btn-primary">' in faixa and "Pagar agora" in faixa


def test_faixa_sem_cobranca_sem_boleto(client, db, carla, pedro):
    plano = _reservado(db, pedro, carla, SEM_BOLETO)
    faixa = re.search(r'<section class="plano-urgente".*?</section>', _html(client.get(f"/planos/{plano.id}"))).group(0)
    assert ("Restam 3h 40min. Com Pix, o pagamento também vale até esse horário. Sem cobrança, os horários voltam a "
            "ficar livres e o plano expira.") in faixa
    assert "boleto" not in faixa


def test_faixa_com_boleto_gerado_usa_pague_ate(client, db, carla, pedro, api_falsa):
    plano = _reservado(db, pedro, carla)
    api_falsa.responder("/transparents/create", cobranca_boleto(valor=60000))
    assert client.post(f"/planos/{plano.id}/pagamento", data={"forma": "boleto"}, follow_redirects=False).status_code == 303
    faixa = re.search(r'<section class="plano-urgente".*?</section>', _html(client.get(f"/planos/{plano.id}"))).group(0)
    assert "Pague até <strong>06/08, às 23:59</strong>" in faixa and "Gere a cobrança" not in faixa


def test_forma_de_pagamento_sem_horas_escritas(client, db, carla, pedro):
    html = _html(client.get(f"/planos/{_reservado(db, pedro, carla).id}/pagamento"))
    assert ("A reserva vale até <strong>hoje, às 14:00</strong> (horário de Brasília). <span class=\"small\">Se não gerar a "
            "cobrança até lá, os horários voltam a ficar livres.</span>") in html
    assert ('Pague até <strong class="pag-prazo">hoje, às 14:00</strong>. QR Code ou copia e cola; a confirmação costuma '
            "chegar em segundos.") in html
    assert "Vence em 06/08/2026. Ao gerar o boleto, o horário fica reservado até o vencimento." in html
    assert "horas" not in html.split("<main", 1)[1]


def test_inicio_sem_cobranca_e_com_pix(client, db, carla, pedro, maria, api_falsa):
    _reservado(db, pedro, carla)
    ana = criar_paciente(db, maria.id, nome="Ana Souza")
    pix = _reservado(db, ana, carla, date(2026, 8, 17))
    api_falsa.responder("/transparents/create", cobranca_pix(valor=60000))
    assert client.post(f"/planos/{pix.id}/pagamento", data={"forma": "pix"}, follow_redirects=False).status_code == 303
    bloco = re.search(r'<section class="ini-pagar".*?</section>', _html(client.get("/"))).group(0)
    assert "Gere a cobrança até hoje, às 14:00 (restam 3h 40min)." in bloco
    assert "Pague até hoje, às 14:00 (restam 3h 40min)." in bloco


def test_revisao_mantem_as_linhas_pix_e_boleto(client, carla, pedro):
    prazo = re.search(r'<div class="rev-prazo" role="note" id="aviso-prazo">.*?</div>', _revisao(client, carla, pedro)).group(0)
    assert "Depois de confirmar, é preciso pagar dentro do prazo." in prazo
    # O prazo previsto conta de agora (10:20), não de um plano já criado
    assert "<li>Pix: pague até hoje, às 14:20.</li>" in prazo and "<li>Boleto: vence em 06/08/2026.</li>" in prazo
    assert prazo.count("Depois de confirmar") == 1


def test_renovacao_cita_o_boleto_so_quando_oferecido(client, db, monkeypatch, carla, pedro):
    url = f"/planos/{_encerrado(db, pedro, carla).id}/renovar"
    assert "você terá 4 horas para gerar a cobrança; o boleto vale até o vencimento." in _html(client.get(url))
    monkeypatch.setattr(cliente_router, "agora_local", lambda: datetime(2026, 8, 6, 9, 0))  # 1ª segunda: 10/08, sem boleto
    monkeypatch.setattr(cliente_router, "agora_utc", lambda: datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc))
    html = _html(client.get(url))
    assert "você terá 4 horas para gerar a cobrança." in html and "o boleto vale até o vencimento" not in html


def test_admin_usa_o_texto_neutro(client, db, carla, pedro, maria):
    _reservado(db, pedro, carla)
    criar_admin(db)
    logar(client, "admin@exemplo.com")
    html = _html(client.get(f"/admin/clientes/{maria.id}"))
    assert '<p class="cli-plano-prazo">Reserva até hoje, às 14:00 (sem cobrança gerada)</p>' in html
    assert "Gere a cobrança" not in html and "Pague até" not in html


def test_todas_as_telas_usam_a_mesma_funcao(client, db, monkeypatch, carla, pedro, maria):
    chamadas = []

    def falsa(criado_em, inicio, forma, agora):
        chamadas.append(forma)
        return TextoPrazo("ACAO-X", "QUANDO-X", "RESTAM-X", datetime(2026, 8, 3, 14, 0), forma is None,
                          date(2026, 8, 6), "NEUTRO-X")

    for modulo in (contratacao_service, inicio_cliente_service, admin_clientes_service):
        monkeypatch.setattr(modulo, "texto_prazo_pagamento", falsa)
    plano = _reservado(db, pedro, carla)
    encerrado = _encerrado(db, criar_paciente(db, maria.id, nome="Ana Souza"), carla)
    telas = {
        "plano": (f"/planos/{plano.id}", "ACAO-X <strong>QUANDO-X</strong>"),
        "pagamento": (f"/planos/{plano.id}/pagamento", "A reserva vale até <strong>QUANDO-X</strong>"),
        "inicio": ("/", "ACAO-X QUANDO-X (restam RESTAM-X)."),
        "renovacao": (f"/planos/{encerrado.id}/renovar", "o boleto vale até o vencimento"),
    }
    for nome, (url, trecho) in telas.items():
        assert trecho in _html(client.get(url)), nome
    assert "<li>Pix: pague até QUANDO-X.</li>" in _revisao(client, carla, pedro)
    criar_admin(db)
    logar(client, "admin@exemplo.com")
    assert '<p class="cli-plano-prazo">NEUTRO-X</p>' in _html(client.get(f"/admin/clientes/{maria.id}"))
    assert len(chamadas) == 6
