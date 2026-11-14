import re
from datetime import date, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models.avaliacao import Avaliacao
from app.models.pagamento import FormaPagamento, Pagamento, StatusPagamento
from app.models.sessao import Sessao, StatusSessao
from app.routers import consultas as consultas_router
from app.services import consultas_service, remarcacao_service
from app.services.contratacao_service import texto_prazo_pagamento
from tests.fabricas import criar_cliente, criar_especialidade, criar_funcionario, criar_paciente, criar_plano, logar

AGENDADA = StatusSessao.AGENDADA
PENDENTE = StatusSessao.PENDENTE_PAGAMENTO
REALIZADA = StatusSessao.REALIZADA

AGORA = datetime(2026, 11, 3, 10, 0)  # terça, horário local
AGORA_UTC = datetime(2026, 11, 3, 13, 0)
CRIADO_PAGO = datetime(2026, 10, 1, 12, 0)
CRIADO_9H = datetime(2026, 11, 3, 12, 0)  # UTC = 09:00 local; Pix até 13:00
CRIADO_10H30 = datetime(2026, 11, 3, 13, 30)  # UTC = 10:30 local; Pix até 14:30
QUI = datetime(2026, 11, 5, 14, 0)
TER = datetime(2026, 11, 10, 14, 0)
RAIZ = Path(__file__).resolve().parent.parent
HORAS_RN08 = (8, 9, 10, 11, 14, 15, 16, 17)


@pytest.fixture(autouse=True)
def relogio_fixo(monkeypatch):
    monkeypatch.setattr(consultas_router, "agora_local", lambda: AGORA)
    monkeypatch.setattr(consultas_router, "agora_utc", lambda: AGORA_UTC)


@pytest.fixture
def fono(db):
    return criar_funcionario(db, criar_especialidade(db, "Fonoaudiologia"))


@pytest.fixture
def cliente(db):
    return criar_cliente(db)


@pytest.fixture
def pedro(db, cliente):
    return criar_paciente(db, cliente.id, nome="Pedro Souza")


@pytest.fixture
def logado(client, cliente):
    logar(client, cliente.email)
    return client


def _html(resposta, codigo=200) -> str:
    assert resposta.status_code == codigo, resposta.status_code
    return " ".join(resposta.text.split())


def _sessao(db, data_hora) -> Sessao:
    return db.scalar(select(Sessao).where(Sessao.data_hora == data_hora))


def _ocupar(db, funcionario, horarios, email="outro@exemplo.com"):
    outro = criar_cliente(db, email=email)
    criar_plano(db, criar_paciente(db, outro.id, nome="Outro Atendido"), funcionario, [(h, AGENDADA) for h in horarios],
                criado_em=CRIADO_PAGO)


def _ambar(html: str) -> str:
    return re.search(r'<section class="ini-pagar cons-pagar".*?</section>', html).group(0)


# --- Funções puras (RNF14) ---------------------------------------------------------------------------------

@pytest.mark.parametrize("dia, esperado", [
    (date(2026, 11, 5), "Novembro de 2026"), (date(2027, 3, 1), "Março de 2027"), (date(2026, 12, 31), "Dezembro de 2026"),
])
def test_titulo_do_mes_em_portugues_sem_locale(dia, esperado):
    assert consultas_service.titulo_mes(dia) == esperado


def test_agrupa_por_mes_na_ordem_da_lista():
    def consulta(dia):
        return consultas_service.ConsultaCliente(1, dia, dia, "A", "P", "E", AGENDADA, False, None)
    grupos = consultas_service.agrupar_por_mes([consulta(QUI), consulta(TER), consulta(datetime(2026, 12, 1, 9, 0))])
    assert [(g.titulo, len(g.consultas)) for g in grupos] == [("Novembro de 2026", 2), ("Dezembro de 2026", 1)]


@pytest.mark.parametrize("status, comparecimento, fim, esperado", [
    (REALIZADA, True, datetime(2026, 11, 2, 10, 0), "Realizada"),
    (REALIZADA, None, datetime(2026, 11, 2, 10, 0), "Realizada"),
    (REALIZADA, False, datetime(2026, 11, 2, 10, 0), "Não compareceu"),
    (AGENDADA, None, datetime(2026, 11, 3, 10, 0), "Aguardando registro do profissional"),  # terminou agora
    (AGENDADA, None, datetime(2026, 11, 3, 10, 30), "Em andamento"),
])
def test_estado_da_consulta_anterior(status, comparecimento, fim, esperado):
    assert consultas_service.estado_anterior(status, comparecimento, fim, AGORA) == esperado


def test_frase_da_troca_com_e_sem_horario_escolhido():
    atual = datetime(2026, 11, 17, 9, 0)
    assert remarcacao_service.texto_troca(atual) == "Você vai trocar a consulta de terça, 17/11, às 09:00 pelo horário escolhido."
    assert remarcacao_service.texto_troca(atual, datetime(2026, 11, 18, 10, 0)) == (
        "Você vai trocar a consulta de terça, 17/11, às 09:00 pelo horário escolhido (quarta, 18/11, às 10:00)."
    )
    assert remarcacao_service.texto_nada_alterado(atual) == "Nada foi alterado. A consulta continua na terça, 17/11, às 09:00."


# --- Consultas: abas, filtro e linhas -------------------------------------------------------------------------

def test_abas_contam_so_as_sessoes_da_lista(db, logado, pedro, fono):
    criar_plano(db, pedro, fono, [(QUI, AGENDADA), (TER, AGENDADA), (datetime(2026, 10, 27, 9, 0), REALIZADA)], criado_em=CRIADO_PAGO)
    criar_plano(db, pedro, fono, [(datetime(2026, 11, 6, 9, 0), PENDENTE), (datetime(2026, 11, 13, 9, 0), PENDENTE)],
                criado_em=CRIADO_9H)
    html = _html(logado.get("/consultas"))
    assert 'aria-current="page">Próximas <span class="font-monospace-num">2</span>' in html
    assert '>Anteriores <span class="font-monospace-num">1</span>' in html
    assert html.count('class="consulta-item') == 2


def test_filtro_so_aparece_com_mais_de_um_atendido(db, logado, cliente, pedro, fono):
    criar_plano(db, pedro, fono, [(QUI, AGENDADA)], criado_em=CRIADO_PAGO)
    assert "cons-filtro" not in _html(logado.get("/consultas"))
    criar_paciente(db, cliente.id, nome="Ana Souza")
    filtro = re.search(r'<nav class="cons-filtro".*?</nav>', _html(logado.get("/consultas"))).group(0)
    assert 'class="btn btn-sm rounded-pill btn-primary" aria-current="page">Todos</a>' in filtro
    assert ">Ana Souza</a>" in filtro and ">Pedro Souza</a>" in filtro


def test_filtro_por_atendido_e_troca_de_aba_mantem_o_filtro(db, logado, cliente, pedro, fono):
    ana = criar_paciente(db, cliente.id, nome="Ana Souza")
    criar_plano(db, pedro, fono, [(QUI, AGENDADA)], criado_em=CRIADO_PAGO)
    criar_plano(db, ana, fono, [(TER, AGENDADA)], criado_em=CRIADO_PAGO)
    html = _html(logado.get("/consultas", params={"atendido": str(ana.id)}))
    assert html.count('class="consulta-item') == 1 and "Ana Souza</span>" in html and "Pedro Souza</span> <span class=\"text-muted" not in html
    assert f'href="/consultas?aba=anteriores&amp;atendido={ana.id}"' in html
    assert f'aria-current="page">Ana Souza</a>' in html
    anteriores = _html(logado.get("/consultas", params={"aba": "anteriores", "atendido": str(ana.id)}))
    assert f'href="/consultas?aba=proximas&amp;atendido={ana.id}"' in anteriores


@pytest.mark.parametrize("valor", ["999", "abc", "²", "-1", "1.5", "99999999999999999999"])
def test_filtro_invalido_responde_404(db, logado, pedro, valor):
    assert logado.get("/consultas", params={"atendido": valor}).status_code == 404


def test_filtro_com_atendido_de_outro_cliente_responde_404(db, logado, pedro):
    alheio = criar_paciente(db, criar_cliente(db, email="outra@exemplo.com").id, nome="Alheio")
    assert logado.get("/consultas", params={"atendido": str(alheio.id)}).status_code == 404


def test_linha_com_dia_mes_quando_selo_e_remarcar(db, logado, pedro, fono):
    criar_plano(db, pedro, fono, [(TER, AGENDADA)], criado_em=CRIADO_PAGO)
    _sessao(db, TER).foi_remarcada = True
    db.commit()
    html = _html(logado.get("/consultas"))
    assert '<h2 class="cons-mes">Novembro de 2026</h2>' in html
    linha = re.search(r'<li class="consulta-item cons-linha esp-fonoaudiologia">.*?</li>', html).group(0)
    assert '<span class="cons-dia-semana" aria-hidden="true">ter.</span>' in linha
    assert '<span class="cons-dia-num" aria-hidden="true">10</span>' in linha
    assert "14:00 às 15:00</span> · <span class=\"cons-quando\">em 7 dias</span>" in linha
    assert 'status-remarcada">Remarcada<' in linha
    assert f'href="/consultas/{_sessao(db, TER).id}/remarcar"' in linha and ">Remarcar</a>" in linha


def test_especialidade_sem_cor_usa_esp_neutra(db, logado, pedro):
    musico = criar_funcionario(db, criar_especialidade(db, "Musicoterapia"), email="m@exemplo.com", nome="Mara Música")
    criar_plano(db, pedro, musico, [(QUI, AGENDADA)], criado_em=CRIADO_PAGO)
    assert '<li class="consulta-item cons-linha esp-neutra">' in _html(logado.get("/consultas"))


def test_paginacao_so_das_agendadas(db, logado, pedro, fono):
    agendadas = [(datetime(2026, 11, 4, 8, 0).replace(day=4 + i // 8, hour=HORAS_RN08[i % 8]), AGENDADA) for i in range(22)]
    criar_plano(db, pedro, fono, agendadas, criado_em=CRIADO_PAGO)
    criar_plano(db, pedro, fono, [(datetime(2026, 11, 20 + i, 9, 0), PENDENTE) for i in range(3)], criado_em=CRIADO_9H)
    primeira = _html(logado.get("/consultas"))
    assert primeira.count('class="consulta-item') == 20 and "Página 1 de 2" in primeira
    segunda = _html(logado.get("/consultas", params={"pagina": "2"}))
    assert segunda.count('class="consulta-item') == 2 and "3 sessões aguardando pagamento" in segunda


# --- Bloco âmbar das sessões aguardando pagamento -----------------------------------------------------------

def test_bloco_ambar_um_grupo_por_plano_com_o_prazo_da_funcao_unica(db, logado, pedro, fono):
    sem_cobranca = criar_plano(db, pedro, fono, [(datetime(2026, 11, 4, 9, 0), PENDENTE), (datetime(2026, 11, 11, 9, 0), PENDENTE)],
                               criado_em=CRIADO_9H)
    pix = criar_plano(db, pedro, fono, [(datetime(2026, 11, 6, 9, 0), PENDENTE)], criado_em=CRIADO_10H30)
    boleto = criar_plano(db, pedro, fono, [(datetime(2026, 11, 16, 9, 0), PENDENTE)], criado_em=CRIADO_9H)
    db.add_all([Pagamento(plano_id=pix.id, forma_pagamento=FormaPagamento.PIX, status=StatusPagamento.PENDENTE),
                Pagamento(plano_id=boleto.id, forma_pagamento=FormaPagamento.BOLETO, status=StatusPagamento.PENDENTE)])
    db.commit()
    ambar = _ambar(_html(logado.get("/consultas")))
    assert '<h2 class="ini-pagar-titulo" id="titulo-aviso-pendente">4 sessões aguardando pagamento</h2>' in ambar
    textos = re.findall(r'<span class="ini-pagar-prazo">(.*?)</span>', ambar)
    assert textos == [
        "2 sessões, a primeira em 04/11. Gere a cobrança até hoje, às 13:00 (restam 3h).",
        "1 sessão, a primeira em 06/11. Pague até hoje, às 14:30 (restam 4h 30min).",
        "1 sessão, a primeira em 16/11. Pague até 06/11, às 23:59.",
    ]
    for plano, forma in ((sem_cobranca, None), (pix, FormaPagamento.PIX), (boleto, FormaPagamento.BOLETO)):
        assert texto_prazo_pagamento(plano.criado_em, plano.data_inicio, forma, AGORA).linha in ambar
        assert f'href="/planos/{plano.id}/pagamento"' in ambar
    assert ambar.count(">Pagar agora<") == 3
    assert "horas" not in ambar


def test_bloco_ambar_nao_e_paginado_e_fica_no_fim(db, logado, pedro, fono):
    criar_plano(db, pedro, fono, [(QUI, AGENDADA)], criado_em=CRIADO_PAGO)
    criar_plano(db, pedro, fono, [(datetime(2026, 11, 4 + i, 15, 0), PENDENTE) for i in range(25)], criado_em=CRIADO_9H)
    html = _html(logado.get("/consultas"))
    assert _ambar(html).count("<li>") == 1 and "25 sessões aguardando pagamento" in html
    assert html.index('class="consulta-item') < html.index('id="aviso-pagamento-pendente"')


def test_sem_pendentes_o_bloco_nao_aparece(db, logado, pedro, fono):
    criar_plano(db, pedro, fono, [(QUI, AGENDADA)], criado_em=CRIADO_PAGO)
    assert "cons-pagar" not in _html(logado.get("/consultas"))


def test_filtro_tambem_filtra_o_bloco_ambar(db, logado, cliente, pedro, fono):
    ana = criar_paciente(db, cliente.id, nome="Ana Souza")
    criar_plano(db, pedro, fono, [(datetime(2026, 11, 4, 9, 0), PENDENTE)], criado_em=CRIADO_9H)
    html = _html(logado.get("/consultas", params={"atendido": str(ana.id)}))
    assert "cons-pagar" not in html


# --- Anteriores e estados vazios ----------------------------------------------------------------------------

def test_anteriores_com_os_tres_estados_e_nota_do_historico(db, logado, cliente, pedro, fono):
    criar_paciente(db, cliente.id, nome="Ana Souza")
    realizada, falta, sem_registro = datetime(2026, 10, 20, 9, 0), datetime(2026, 10, 27, 9, 0), datetime(2026, 11, 2, 9, 0)
    criar_plano(db, pedro, fono, [(realizada, REALIZADA), (falta, REALIZADA), (sem_registro, AGENDADA)], criado_em=CRIADO_PAGO)
    db.add_all([Avaliacao(sessao_id=_sessao(db, realizada).id, arquivo_url="a.pdf", comparecimento=True),
                Avaliacao(sessao_id=_sessao(db, falta).id, comparecimento=False)])
    db.commit()
    html = _html(logado.get("/consultas", params={"aba": "anteriores"}))
    assert 'status-inativo">Realizada<' in html and 'status-nao-compareceu">Não compareceu<' in html
    assert 'status-pendente">Aguardando registro do profissional<' in html
    assert ">Remarcar</a>" not in html
    assert 'As avaliações ficam no <a href="/historico">Histórico</a>.' in html
    filtrado = _html(logado.get("/consultas", params={"aba": "anteriores", "atendido": str(pedro.id)}))
    assert f'<a href="/historico?atendido={pedro.id}">Histórico</a>' in filtrado
    assert [m for m in re.findall(r'<h2 class="cons-mes">(.*?)</h2>', html)] == ["Novembro de 2026", "Outubro de 2026"]


def test_vazio_sem_atendido_leva_a_cadastrar(logado):
    vazio = re.search(r'<section class="cartao estado-vazio" id="consultas-vazio">.*?</section>', _html(logado.get("/consultas"))).group(0)
    assert 'href="/pacientes/novo"' in vazio and "Cadastrar atendido" in vazio


def test_vazio_com_atendido_filtrado_sem_plano(db, logado, cliente, pedro, fono):
    ana = criar_paciente(db, cliente.id, nome="Ana Clara Souza")
    criar_plano(db, pedro, fono, [(QUI, AGENDADA)], criado_em=CRIADO_PAGO)
    html = _html(logado.get("/consultas", params={"atendido": str(ana.id)}))
    assert f'<a href="/especialidades?atendido={ana.id}" class="btn btn-primary">Contratar plano para Ana</a>' in html
    assert "Ana Clara Souza ainda não tem plano." in html


def test_vazio_com_atendido_sem_filtro_leva_a_contratar(db, logado, pedro):
    vazio = re.search(r'id="consultas-vazio">.*?</section>', _html(logado.get("/consultas"))).group(0)
    assert '<a href="/especialidades" class="btn btn-primary">Contratar plano</a>' in vazio


def test_so_pendentes_mostra_frase_curta_e_o_bloco(db, logado, pedro, fono):
    criar_plano(db, pedro, fono, [(datetime(2026, 11, 4, 9, 0), PENDENTE)], criado_em=CRIADO_9H)
    html = _html(logado.get("/consultas"))
    assert 'id="sem-confirmadas"' in html and "consultas-vazio" not in html and "cons-pagar" in html


# --- Remarcar consulta ------------------------------------------------------------------------------------

@pytest.fixture
def consulta(db, pedro, fono):
    criar_plano(db, pedro, fono, [(QUI, AGENDADA), (TER, AGENDADA)], criado_em=CRIADO_PAGO)
    return _sessao(db, QUI)


def test_ordem_da_tela_e_cartao_da_consulta_atual(db, logado, consulta):
    html = _html(logado.get(f"/consultas/{consulta.id}/remarcar", params={"data": "2026-11-06"}))
    ordem = ["Consulta atual", "Escolha a nova data", 'type="date"', "Ver horários", "Horários livres na sexta, 06/11",
             'id="texto-troca"', "Confirmar remarcação"]
    posicoes = [html.index(t) for t in ordem]
    assert posicoes == sorted(posicoes)  # o cartão vem antes no HTML: no celular fica no topo
    regras = re.search(r'<ul class="remarcar-regras" id="regras-remarcacao">(.*?)</ul>', html).group(1)
    assert re.findall(r"<li>(.*?)</li>", regras) == [
        "Até 4 horas antes da consulta.",
        'Novas datas até <span class="font-monospace-num">02/01/2027</span>.',
        "Só horários livres do profissional, de segunda a sexta, fora de feriados nacionais.",
        "As outras sessões do plano não mudam.",
    ]
    atual = re.search(r'<aside class="cartao consulta-atual.*?</aside>', html).group(0)
    assert "Pedro Souza" in atual and "com Carlos Fono" in atual and "Fonoaudiologia" in atual
    assert "Quinta, <span class=\"font-monospace-num\">05/11/2026</span>" in atual and "14:00 às 15:00" in atual


def test_horas_da_regra_vem_da_constante(db, logado, consulta, monkeypatch):
    monkeypatch.setattr(consultas_router, "ANTECEDENCIA_REMARCACAO_HORAS", 6)
    assert "Até 6 horas antes da consulta." in _html(logado.get(f"/consultas/{consulta.id}/remarcar"))
    template = (RAIZ / "app" / "templates" / "cliente" / "remarcar.html").read_text(encoding="utf-8")
    assert not re.search(r"\b\d+\s+horas\b", template) and "60 dias" not in template


def test_manha_tarde_com_ocupados_hachurados(db, logado, consulta, fono):
    _ocupar(db, fono, [datetime(2026, 11, 6, 9, 0), datetime(2026, 11, 6, 15, 0)])
    html = _html(logado.get(f"/consultas/{consulta.id}/remarcar", params={"data": "2026-11-06"}))
    turnos = re.findall(r'<legend class="remarcar-turno-rotulo">(\w+)</legend> <div class="remarcar-celulas">(.*?)</div>', html)
    assert [t for t, _ in turnos] == ["Manhã", "Tarde"]
    assert [c.count('name="novo_horario"') for _, c in turnos] == [4, 4]
    ocupado = re.search(r'<input[^>]*id="h-9"[^>]*>\s*<label class="celula celula-ocupada" for="h-9">.*?</label>', html).group(0)
    assert "disabled" in ocupado and '<span class="celula-indisponivel">ocupado</span>' in ocupado
    assert html.count("celula celula-ocupada") == 2
    assert '<label class="celula " for="h-10">' in html


def test_service_devolve_os_8_horarios_com_estado(db, consulta, fono, pedro):
    _ocupar(db, fono, [datetime(2026, 11, 6, 9, 0)])
    resumo = remarcacao_service.buscar_sessao_do_cliente(db, pedro.cliente_id, consulta.id)
    dia = remarcacao_service.horarios_livres(db, resumo, date(2026, 11, 6), AGORA)
    assert [h.data_hora.hour for h in dia.grade] == list(HORAS_RN08)
    assert [h.livre for h in dia.grade] == [True, False, True, True, True, True, True, True]
    assert len(dia.manha) == 4 and len(dia.tarde) == 4 and len(dia.horarios) == 7


def test_dia_lotado_mostra_bloco_cinza_e_os_ocupados(db, logado, consulta, fono):
    _ocupar(db, fono, [datetime(2026, 11, 6, h, 0) for h in HORAS_RN08])
    html = _html(logado.get(f"/consultas/{consulta.id}/remarcar", params={"data": "2026-11-06"}))
    assert ('<div class="rev-feriado remarcar-aviso" role="status" id="horarios-motivo">Não há horários livres neste dia. '
            "Escolha outra data.</div>") in html
    assert html.count("celula celula-ocupada") == 8 and "Horários na sexta, 06/11" in html and "Horários livres na" not in html
    assert re.search(r'class="btn btn-primary d-none" id="btn-confirmar"', html)


def test_frase_da_troca_antes_do_botao(db, logado, consulta):
    html = _html(logado.get(f"/consultas/{consulta.id}/remarcar", params={"data": "2026-11-06"}))
    assert ('<p class="remarcar-troca" id="texto-troca">Você vai trocar a consulta de quinta, 05/11, às 14:00 pelo '
            "horário escolhido.</p>") in html
    assert html.index('id="texto-troca"') < html.index('id="btn-confirmar"')


def test_conflito_409_mostra_bloco_vermelho(db, logado, consulta, fono):
    _ocupar(db, fono, [datetime(2026, 11, 6, 9, 0)])
    resposta = logado.post(f"/consultas/{consulta.id}/remarcar", data={"novo_horario": "2026-11-06T09:00"})
    html = _html(resposta, 409)
    conflito = re.search(r'<div class="rev-conflito remarcar-aviso" role="alert" id="aviso-conflito">.*?</div>', html).group(0)
    assert "Nada foi alterado. A consulta continua na quinta, 05/11, às 14:00." in conflito
    assert f'href="/consultas/{consulta.id}/remarcar?data=2026-11-06"' in conflito and "Escolher outro horário" in conflito
    assert _sessao(db, QUI) is not None  # nada mudou


def test_erro_400_com_horario_livre_mostra_o_escolhido_na_frase(db, logado, consulta, monkeypatch):
    def recusar(*args, **kwargs):
        raise remarcacao_service.RemarcacaoInvalida("Erro de teste.")
    monkeypatch.setattr(remarcacao_service, "remarcar", recusar)
    html = _html(logado.post(f"/consultas/{consulta.id}/remarcar", data={"novo_horario": "2026-11-06T10:00"}), 400)
    assert "pelo horário escolhido (sexta, 06/11, às 10:00)." in html and "Erro de teste." in html
    assert "aviso-conflito" not in html


def test_endpoint_json_nao_mudou(db, logado, consulta, fono):
    _ocupar(db, fono, [datetime(2026, 11, 6, 9, 0)])
    dados = logado.get(f"/consultas/{consulta.id}/horarios", params={"data": "2026-11-06"}).json()
    assert set(dados) == {"data", "horarios", "motivo"} and len(dados["horarios"]) == 7
    assert dados["horarios"][0] == {"valor": "2026-11-06T08:00", "rotulo": "08:00"}


def test_js_continua_com_um_fetch_e_marca_os_ocupados(db, logado, consulta):
    js = (RAIZ / "app" / "static" / "js" / "remarcar.js").read_text(encoding="utf-8")
    assert js.count("fetch(") == 1 and "celula-ocupada" in js and "dataset.horas" in js
    html = _html(logado.get(f"/consultas/{consulta.id}/remarcar"))
    assert 'data-horas="8,9,10,11,14,15,16,17"' in html
    assert sorted(p.name for p in (RAIZ / "app" / "static" / "js").glob("*.js")) == ["contratar.js", "remarcar.js"]


def test_remarcar_de_outro_cliente_continua_404(db, client, consulta):
    logar(client, criar_cliente(db, email="intruso@exemplo.com").email)
    assert client.get(f"/consultas/{consulta.id}/remarcar").status_code == 404
