import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import event

from app.models.cliente import Cliente
from app.models.pagamento import FormaPagamento, Pagamento, StatusPagamento
from app.models.plano import Plano
from app.models.sessao import StatusSessao
from app.routers import cliente as cliente_router
from app.services import plano_service
from tests.abacatepay_falsa import cobranca_pix
from tests.fabricas import (
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
JULHO = [(datetime(2026, 7, dia, 14, 0), REALIZADA) for dia in (6, 13, 20, 27)]  # segundas, 14:00
CSS = (Path(__file__).resolve().parent.parent / "app" / "static" / "css" / "styles.css").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def relogio_fixo(monkeypatch):
    monkeypatch.setattr(cliente_router, "agora_local", lambda: AGORA)
    monkeypatch.setattr(cliente_router, "agora_utc", lambda: datetime(2026, 8, 1, 13, 0, tzinfo=timezone.utc))


@pytest.fixture
def fono(db):
    return criar_especialidade(db, "Fonoaudiologia", "150.00")


@pytest.fixture
def carla(db, fono):
    return criar_funcionario(db, fono, email="carla@exemplo.com", nome="Carla Fono")


@pytest.fixture
def maria(client, db):
    usuario = criar_cliente(db, email="maria@exemplo.com", nome="Maria Souza")
    db.get(Cliente, usuario.id).cpf = "529.982.247-25"
    db.commit()
    logar(client, usuario.email)
    return usuario


@pytest.fixture
def pedro(db, maria):
    return criar_paciente(db, maria.id, nome="Pedro Souza")


@pytest.fixture
def encerrado(db, pedro, carla):
    return criar_plano(db, pedro, carla, JULHO, valor_total="600.00")


def _html(resposta) -> str:
    assert resposta.status_code == 200, resposta.status_code
    return " ".join(resposta.text.split())


def _linha(html: str, rotulo: str) -> tuple[str, str]:
    linha = re.search(rf'<tr[^>]*> <th scope="row">{rotulo}</th> (.*?)</tr>', html).group(1)
    antes = re.search(r'<td data-rotulo="Antes"[^>]*>(.*?)</td>', linha).group(1)
    agora = re.search(r'<td data-rotulo="Agora"[^>]*>(.*?)</td>', linha).group(1)
    return antes.strip(), agora.strip()


# --- Página curta -------------------------------------------------------------------------------------

def test_pagina_curta_com_cabecalho_e_continua_igual(client, encerrado):
    html = _html(client.get(f"/planos/{encerrado.id}/renovar"))
    assert "<h1" in html and "Renovar plano" in html
    assert 'class="plano-cab esp-fonoaudiologia"' in html and "<strong>Pedro Souza</strong>" in html
    assert "O plano anterior terminou em 27/07/2026 e não muda." in html
    continua = re.search(r'id="renovacao-continua">(.*?)</section>', html).group(1)
    assert re.findall(r"<dt>(.*?)</dt>", continua) == ["Profissional", "Dias e horário", "Frequência e duração"]
    assert "<dd>Carla Fono</dd>" in continua and "<dd>Segunda, às 14:00 (60 minutos)</dd>" in continua
    assert "<dd>1x por semana · Mensal (1 mês)</dd>" in continua
    assert 'id="form-contratacao"' not in html and "etapas" not in html.split("<main", 1)[1]


def test_tabela_antes_e_agora(client, encerrado):
    html = _html(client.get(f"/planos/{encerrado.id}/renovar"))
    assert re.search(r'<th scope="col">Antes</th><th scope="col">Agora</th>', html.replace("> <", "><"))
    assert _linha(html, "Período") == ("06/07/2026 a 27/07/2026", "03/08/2026 a 31/08/2026")
    assert _linha(html, "Sessões") == ("4", "5")
    assert _linha(html, "Valor da sessão") == ("R$ 150,00", "R$ 150,00")
    antes, agora = _linha(html, "Valor total")
    assert antes == "R$ 600,00"
    assert '<span class="renov-total-novo font-monospace-num">R$ 712,50</span>' in agora
    assert "Valor cheio R$ 750,00 · Desconto de 5%: " in agora and "− R$ 37,50" in agora


def test_valores_recalculados_com_tabela_e_valor_atuais(client, db, monkeypatch, fono, encerrado):
    monkeypatch.setitem(plano_service.DESCONTO_POR_DURACAO, "mensal", 12)
    fono.valor_sessao = Decimal("200.00")
    db.commit()
    html = _html(client.get(f"/planos/{encerrado.id}/renovar"))
    assert _linha(html, "Valor da sessão") == ("R$ 150,00", "R$ 200,00")
    antes, agora = _linha(html, "Valor total")
    assert antes == "R$ 600,00"
    assert "R$ 880,00" in agora and "Valor cheio R$ 1.000,00 · Desconto de 12%" in agora and "− R$ 120,00" in agora


def test_sem_desconto_nao_mostra_a_linha(client, db, monkeypatch, encerrado):
    monkeypatch.setitem(plano_service.DESCONTO_POR_DURACAO, "mensal", 0)
    html = _html(client.get(f"/planos/{encerrado.id}/renovar"))
    assert "renovacao-desconto" not in html and "R$ 750,00" in _linha(html, "Valor total")[1]


def test_tabela_vira_linhas_no_celular():
    celular = CSS.split("/* --- Renovar plano")[1]
    assert re.search(r"\.renov-comparacao tr \{\s*display: grid;", celular)
    assert 'content: attr(data-rotulo) ": ";' in celular


def test_renovacao_sem_javascript(client, encerrado):
    html = _html(client.get(f"/planos/{encerrado.id}/renovar"))
    assert html.count("<script") == 1  # só o bundle do Bootstrap
    assert "fetch" not in html and "contratar.js" not in html


# --- Ações --------------------------------------------------------------------------------------------

def test_botao_leva_ao_resumo_e_nao_cria_plano(client, db, carla, encerrado):
    html = client.get(f"/planos/{encerrado.id}/renovar").text
    form = re.search(r'<form method="post" action="([^"]+)" id="form-renovar-direto">(.*?)</form>', html, re.S)
    assert form.group(1) == f"/contratar/{carla.usuario.id}"
    assert "Renovar com a mesma configuração" in form.group(2)
    dados = {}
    for nome, valor in re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', form.group(2)):
        dados.setdefault(nome, []).append(valor)
    revisao = client.post(form.group(1), data=dados)
    assert revisao.status_code == 200 and "Confira e confirme" in revisao.text and 'id="form-confirmar"' in revisao.text
    assert db.query(Plano).count() == 1


def test_link_mudar_abre_o_passo_2_preenchido(client, carla, encerrado):
    html = _html(client.get(f"/planos/{encerrado.id}/renovar"))
    assert f'<a href="/planos/{encerrado.id}/renovar?editar=1" class="renov-mudar" id="link-mudar">Mudar dia, horário ou duração</a>' in html
    passo2 = _html(client.get(f"/planos/{encerrado.id}/renovar?editar=1"))
    assert 'id="form-contratacao"' in passo2 and "Renovar plano" in passo2
    assert re.search(r'value="0-14"[^>]*data-livre="1"[^>]*checked', passo2)
    assert 'id="duracao-mensal" value="mensal" autocomplete="off" checked' in passo2
    assert f'href="/planos/{encerrado.id}/renovar"' in passo2  # "Voltar" leva de volta à página curta
    assert "Continua igual" not in passo2


@pytest.mark.parametrize("editar", ["0", "2", "sim", "¹", ""])
def test_editar_desconhecido_vale_a_pagina_curta(client, encerrado, editar):
    html = _html(client.get(f"/planos/{encerrado.id}/renovar", params={"editar": editar}))
    assert "Continua igual" in html and 'id="form-contratacao"' not in html


def test_horario_ocupado_bloco_vermelho_e_escolher_outro_horario(client, db, fono, carla, encerrado):
    criar_plano_com_sessao(db, carla.usuario.id, fono, AGENDADA, data_hora=datetime(2026, 8, 17, 14, 0),
                           email_cliente="joana@exemplo.com")
    html = _html(client.get(f"/planos/{encerrado.id}/renovar"))
    bloco = re.search(r'<div class="rev-conflito" role="alert" id="aviso-renovacao">(.*?)</div>', html).group(1)
    assert "Esse horário não está mais livre." in bloco
    assert "renov-comparacao" not in html and "R$" not in html and "Renovar com a mesma configuração" not in html
    assert f'href="/planos/{encerrado.id}/renovar?editar=1"' in html and "Escolher outro horário" in html
    passo2 = _html(client.get(f"/planos/{encerrado.id}/renovar?editar=1"))
    assert 'id="form-contratacao"' in passo2 and 'id="duracao-mensal" value="mensal" autocomplete="off" checked' in passo2


# --- 404 ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("plano_id", ["999", "0", "abc", "²", "٣", "2147483648", "9" * 30])
def test_plano_inexistente_ou_id_invalido_404(client, encerrado, plano_id):
    assert client.get(f"/planos/{plano_id}/renovar").status_code == 404
    assert client.get(f"/planos/{plano_id}/renovar?editar=1").status_code == 404


def test_plano_de_outro_cliente_404(client, db, encerrado):
    logar(client, criar_cliente(db, email="outra@exemplo.com", nome="Outra").email)
    assert client.get(f"/planos/{encerrado.id}/renovar").status_code == 404
    assert client.get(f"/planos/{encerrado.id}/renovar?editar=1").status_code == 404


@pytest.mark.parametrize("sessoes", [
    [(datetime(2026, 7, 27, 14, 0), REALIZADA), (datetime(2026, 8, 3, 14, 0), AGENDADA)],  # ativo
    [(datetime(2026, 8, 1, 9, 0), REALIZADA)],  # última sessão hoje
])
def test_plano_nao_encerrado_404(client, db, pedro, carla, sessoes):
    plano = criar_plano(db, pedro, carla, sessoes, criado_em=datetime(2026, 7, 1, 12, 0))
    assert client.get(f"/planos/{plano.id}/renovar").status_code == 404


# --- Consultas SQL ------------------------------------------------------------------------------------

def _contar_sql(client, db, url) -> int:
    comandos = []
    ouvinte = lambda *args: comandos.append(args[2])  # noqa: E731
    event.listen(db.get_bind(), "before_cursor_execute", ouvinte)
    try:
        assert client.get(url).status_code == 200
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", ouvinte)
    return len(comandos)


def test_numero_de_consultas_das_telas(client, db, pedro, carla, encerrado, api_falsa):
    pendente = criar_plano(db, pedro, carla, [(datetime(2026, 8, 10, 14, 0), PENDENTE)], criado_em=datetime(2026, 8, 1, 12, 0))
    api_falsa.responder("/transparents/create", cobranca_pix(valor=60000))
    api_falsa.responder("/transparents/list", [cobranca_pix(valor=60000)])
    client.post(f"/planos/{pendente.id}/pagamento", data={"forma": "pix"})
    pago = criar_plano(db, pedro, carla, [(datetime(2026, 8, 3, 9, 0), AGENDADA)], criado_em=datetime(2026, 7, 30, 12, 0))
    db.add(Pagamento(plano_id=pago.id, forma_pagamento=FormaPagamento.PIX, status=StatusPagamento.CONFIRMADO,
                     confirmado_em=datetime(2026, 7, 30, 12, 10)))
    db.commit()
    medidas = {}
    for nome, url in {
        "cobranca": f"/planos/{pendente.id}/cobranca", "plano_pago": f"/planos/{pago.id}",
        "plano_encerrado": f"/planos/{encerrado.id}", "renovacao": f"/planos/{encerrado.id}/renovar",
        "renovacao_editar": f"/planos/{encerrado.id}/renovar?editar=1",
    }.items():
        _contar_sql(client, db, url)  # aquece: a 1ª requisição recarrega o usuário logado
        medidas[nome] = _contar_sql(client, db, url)
    assert medidas == MEDIDAS_DEPOIS


# Antes deste bloco: cobrança 5, plano pago 4, encerrado 4, renovação 7 (a página repetia o passo 2)
# renovacao_editar: 8 -> 7 em 14/11 (o atendido fixo dispensa a lista de atendidos do Cliente)
MEDIDAS_DEPOIS = {"cobranca": 5, "plano_pago": 4, "plano_encerrado": 4, "renovacao": 5, "renovacao_editar": 7}
