import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.models.cliente import Cliente
from app.models.sessao import StatusSessao
from app.routers import cliente as cliente_router
from app.services import contratacao_service
from tests.abacatepay_falsa import BR_CODE, LINHA_DIGITAVEL, URL_BOLETO, cobranca_boleto, cobranca_pix
from tests.fabricas import criar_cliente, criar_especialidade, criar_funcionario, criar_paciente, criar_plano, logar

PENDENTE = StatusSessao.PENDENTE_PAGAMENTO
AGORA = datetime(2026, 8, 3, 10, 20)  # segunda, horário local
CRIADO_10H = datetime(2026, 8, 3, 13, 0)  # UTC = 10:00 local: o Pix vence às 14:00
CPF = "529.982.247-25"
RAIZ = Path(__file__).resolve().parent.parent
CSS = (RAIZ / "app" / "static" / "css" / "styles.css").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def relogio_fixo(monkeypatch):
    _relogio(monkeypatch, AGORA)


def _relogio(monkeypatch, agora):
    monkeypatch.setattr(cliente_router, "agora_local", lambda: agora)
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


@pytest.fixture
def plano(db, pedro, carla):
    return criar_plano(db, pedro, carla, [(datetime(2026, 8, 14, 14, 0), PENDENTE), (datetime(2026, 8, 21, 14, 0), PENDENTE)],
                       criado_em=CRIADO_10H)


def _html(resposta) -> str:
    assert resposta.status_code == 200, resposta.status_code
    return " ".join(resposta.text.split())


def _cobranca(client, plano, api_falsa, forma="pix", dev_mode=True):
    fabrica = cobranca_pix if forma == "pix" else cobranca_boleto
    api_falsa.responder("/transparents/create", fabrica(valor=60000, dev_mode=dev_mode))
    api_falsa.responder("/transparents/list", [fabrica(valor=60000, dev_mode=dev_mode)])
    assert client.post(f"/planos/{plano.id}/pagamento", data={"forma": forma}, follow_redirects=False).status_code == 303
    return _html(client.get(f"/planos/{plano.id}/cobranca"))


def _cartao_valor(html: str) -> str:
    return re.search(r'<aside class="cartao resumo-plano" aria-labelledby="titulo-valor-cobranca">.*?</aside>', html).group(0)


def _main(html: str) -> str:
    return html.split("<main", 1)[1].split("</main>", 1)[0]


# --- Prazo: funções puras (RNF14) ---------------------------------------------------------------------------

@pytest.mark.parametrize("prazo, esperado", [
    (datetime(2026, 8, 3, 14, 0), "hoje, às 14:00"),
    (datetime(2026, 8, 4, 9, 30), "amanhã, às 09:30"),
    (datetime(2026, 8, 6, 23, 59, 59), "06/08, às 23:59"),
])
def test_prazo_no_formato_unico(prazo, esperado):
    assert contratacao_service.texto_dia_hora(prazo, AGORA) == esperado


@pytest.mark.parametrize("prazo, esperado", [
    (datetime(2026, 8, 3, 14, 0), "3h 40min"),
    (datetime(2026, 8, 3, 10, 21), "1min"),
    (datetime(2026, 8, 3, 10, 20), None),  # exatamente no prazo: nada a mostrar
    (datetime(2026, 8, 3, 9, 0), None),  # vencido: nunca contagem negativa
])
def test_restam_por_funcao_pura(prazo, esperado):
    assert contratacao_service.tempo_restante(prazo, AGORA) == esperado


# --- Cartão "Valor" -----------------------------------------------------------------------------------------

def test_cartao_valor_sem_o_selo_duplicado(client, plano, maria, api_falsa):
    html = _cobranca(client, plano, api_falsa)
    cartao = _cartao_valor(html)
    assert '<h2 class="cartao-titulo" id="titulo-valor-cobranca">Valor</h2>' in cartao
    assert "badge" not in cartao and "Situação" not in html
    assert html.count(">Aguardando pagamento</span>") == 1  # só o do cabeçalho
    assert "<dt>Forma</dt><dd>Pix</dd>" in cartao and "R$ 600,00" in cartao


def test_cartao_valor_do_pix_com_pague_ate_e_restam(client, plano, maria, api_falsa):
    cartao = _cartao_valor(_cobranca(client, plano, api_falsa))
    assert "Pague até <strong>hoje, às 14:00</strong>" in cartao
    assert "Restam 3h 40min. Horário de Brasília." in cartao
    assert "Prazo limite" not in cartao


def test_cartao_valor_do_boleto_com_vencimento(client, plano, maria, api_falsa):
    cartao = _cartao_valor(_cobranca(client, plano, api_falsa, "boleto"))
    assert "Vence em <strong>06/08/2026</strong>" in cartao  # 3 dias úteis depois de 03/08 (RN09)
    assert "Restam" not in cartao and "Pague até" not in cartao


def test_prazo_vencido_sem_contagem_negativa(client, monkeypatch, plano, maria, api_falsa):
    _cobranca(client, plano, api_falsa)
    _relogio(monkeypatch, datetime(2026, 8, 3, 15, 0))
    html = _html(client.get(f"/planos/{plano.id}/cobranca"))
    assert "Prazo de pagamento encerrado" in html and "O prazo terminou em 03/08/2026 às 14:00" in html
    assert "Restam" not in html and "Pague até" not in html and 'http-equiv="refresh"' not in html
    assert not re.search(r"-\s*\d+h|-\s*\d+min", html)


# --- Pix --------------------------------------------------------------------------------------------------

def test_pix_com_os_tres_passos_ao_lado_do_qr_code(client, plano, maria, api_falsa):
    html = _cobranca(client, plano, api_falsa)
    bloco = re.search(r'<div class="cob-pix">(.*?)</div>', html).group(1)
    assert bloco.index('class="cobranca-qrcode"') < bloco.index('<ol class="cob-passos" id="passos-pix">')
    assert re.findall(r"<li>(.*?)</li>", bloco) == [
        "Abra o app do seu banco e escolha Pix.",
        "Leia o QR Code ou use o código copia e cola.",
        "Confira o valor e confirme.",
    ]
    # Ordem no celular (a grade empilha na ordem do HTML): QR, passos, código
    assert html.index("cob-passos") < html.index('id="pix-copia-cola"')
    assert f'id="pix-copia-cola" class="cobranca-codigo mb-0"' in html and BR_CODE in html
    assert "Toque no código para selecioná-lo e copie." in html


def test_pix_sem_botao_copiar_nem_javascript(client, plano, maria, api_falsa):
    html = _cobranca(client, plano, api_falsa)
    main = _main(html)
    assert "Copiar" not in html and "copiar" not in main
    assert not re.search(r"<button[^>]*>[^<]*(?:<i[^>]*></i>)?\s*Cop", main)
    assert "<script" not in main and "onclick" not in main and "clipboard" not in html.lower()
    assert html.count("<script") == 1  # só o bundle do Bootstrap
    assert sorted(p.name for p in (RAIZ / "app" / "static" / "js").iterdir()) == ["contratar.js", "remarcar.js"]


def test_codigo_seleciona_tudo_com_um_toque_so_com_css():
    regra = re.search(r"(?m)^\.cobranca-codigo\s*\{([^}]*)\}", CSS).group(1)
    assert "user-select: all;" in regra and "-webkit-user-select: all;" in regra


def test_grade_do_pix_empilha_no_celular():
    assert re.search(r"(?m)^\.cob-pix\s*\{[^}]*grid-template-columns: 220px minmax\(0, 1fr\);", CSS)
    celular = CSS.split("/* --- Cobrança por Pix e por boleto")[1].split("/* ---")[0].split("@media (max-width: 575.98px)")[1]
    assert re.search(r"\.cob-pix\s*\{\s*grid-template-columns: 1fr;", celular)


# --- Boleto -----------------------------------------------------------------------------------------------

def test_boleto_com_linha_digitavel_abrir_boleto_e_aviso_de_um_dia_util(client, plano, maria, api_falsa):
    html = _cobranca(client, plano, api_falsa, "boleto")
    assert "Linha digitável" in html and re.search(rf'id="linha-digitavel"[^>]*>{LINHA_DIGITAVEL}</p>', html)
    assert "Toque no código para selecioná-lo e copie, ou abra o boleto para imprimir." in html
    link = re.search(r'<a href="([^"]+)" target="_blank" rel="([^"]+)"[^>]*>.*?Abrir boleto</a>', html)
    assert link.group(1) == URL_BOLETO and "noopener" in link.group(2).split()
    assert ("<strong>Vence em 06/08/2026.</strong> Depois de pagar, o banco pode levar até 1 dia útil para avisar; "
            "esta página atualiza quando o aviso chegar.") in html


def test_boleto_sem_linha_digitavel_mantem_o_codigo_de_barras(client, plano, maria, api_falsa):
    resposta = cobranca_boleto(valor=60000)
    resposta.pop("identificationNumber", None)
    api_falsa.responder("/transparents/create", resposta)
    api_falsa.responder("/transparents/list", [resposta])
    client.post(f"/planos/{plano.id}/pagamento", data={"forma": "boleto"})
    html = _html(client.get(f"/planos/{plano.id}/cobranca"))
    assert '<p class="form-label" id="rotulo-linha-digitavel">Código de barras</p>' in html


# --- Ambiente de testes, atualização e conferência --------------------------------------------------------

@pytest.mark.parametrize("forma", ["pix", "boleto"])
def test_caixa_tracejada_do_ambiente_de_testes(client, plano, maria, api_falsa, forma):
    html = _cobranca(client, plano, api_falsa, forma)
    assert '<p class="cob-teste" id="ambiente-testes">Ambiente de testes (Dev mode): nenhum valor real é cobrado.</p>' in html
    regra = re.search(r"(?m)^\.cob-teste\s*\{([^}]*)\}", CSS).group(1)
    assert "border: 1px dashed var(--cor-borda-campo);" in regra


def test_sem_dev_mode_nao_mostra_a_caixa(client, plano, maria, api_falsa):
    assert "ambiente-testes" not in _cobranca(client, plano, api_falsa, dev_mode=False)


@pytest.mark.parametrize("forma", ["pix", "boleto"])
def test_atualizacao_de_15_segundos_e_conferencia_intactas(client, monkeypatch, plano, maria, api_falsa, forma):
    html = _cobranca(client, plano, api_falsa, forma)
    assert html.count('<meta http-equiv="refresh" content="15">') == 1
    assert "Esta página se atualiza sozinha a cada 15 segundos." in html
    assert "Depois do pagamento" in html
    # Na atualização seguinte (15 s depois), a página confere a cobrança na AbacatePay (RF06), como antes
    _relogio(monkeypatch, datetime(2026, 8, 3, 10, 20, 15))
    assert client.get(f"/planos/{plano.id}/cobranca").status_code == 200
    assert [r.url.path for r in api_falsa.requisicoes] == ["/v2/transparents/create", "/v2/transparents/list"]
