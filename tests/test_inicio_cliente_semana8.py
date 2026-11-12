import re
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import event

from app.models.pagamento import FormaPagamento, Pagamento, StatusPagamento
from app.models.sessao import Sessao, StatusSessao
from app.routers import inicio as inicio_router
from app.services import inicio_cliente_service as servico
from app.services import inicio_service
from tests.fabricas import criar_cliente, criar_especialidade, criar_funcionario, criar_paciente, criar_plano, logar

AGENDADA = StatusSessao.AGENDADA
PENDENTE = StatusSessao.PENDENTE_PAGAMENTO
REALIZADA = StatusSessao.REALIZADA

AGORA = datetime(2026, 11, 10, 10, 20)  # terça, horário local
CRIADO_10H = datetime(2026, 11, 10, 13, 0)  # UTC = 10:00 local: o Pix vence às 14:00
CRIADO_9H = datetime(2026, 11, 10, 12, 0)  # UTC = 09:00 local: o Pix vence às 13:00


@pytest.fixture(autouse=True)
def relogio_fixo(monkeypatch):
    monkeypatch.setattr(inicio_router, "agora_local", lambda: AGORA)


@pytest.fixture
def carla(db):
    return criar_funcionario(db, criar_especialidade(db, "Psicopedagogia", "180.00"), email="carla@exemplo.com", nome="Carla Mendes")


@pytest.fixture
def rafael(db):
    return criar_funcionario(db, criar_especialidade(db, "Fonoaudiologia", "150.00"), email="rafael@exemplo.com", nome="Rafael Lima")


@pytest.fixture
def maria(client, db):
    cliente = criar_cliente(db, email="maria@exemplo.com", nome="Maria Souza")
    logar(client, cliente.email)
    return cliente


def _html(resposta) -> str:
    assert resposta.status_code == 200
    return " ".join(resposta.text.split())


def _main(html: str) -> str:
    return re.search(r"<main.*?</main>", html).group(0)


def _pagamento(db, plano, forma):
    db.add(Pagamento(plano_id=plano.id, forma_pagamento=forma, status=StatusPagamento.PENDENTE))
    db.commit()


def _resumo(criado_em, data_inicio, forma=None, situacao=inicio_service.AGUARDANDO_PAGAMENTO, plano_id=1):
    plano = SimpleNamespace(id=plano_id, criado_em=criado_em, data_inicio=data_inicio)
    return inicio_service.ResumoPlano(plano, "Pedro", "Fono", "Carla", situacao, 4, 0, None, forma=forma)


# --- Funções puras -----------------------------------------------------------------------------------

@pytest.mark.parametrize("prazo, boleto, agora, esperado", [
    (datetime(2026, 11, 10, 14, 0), False, AGORA, "Pague até 14:00 (restam 3h 40min)"),
    (datetime(2026, 11, 10, 14, 0), False, datetime(2026, 11, 10, 13, 59), "Pague até 14:00 (restam 1min)"),
    (datetime(2026, 11, 11, 1, 0), False, datetime(2026, 11, 10, 22, 0), "Pague até amanhã, 01:00 (restam 3h)"),
    (datetime(2026, 11, 12, 23, 59, 59), True, AGORA, "Pague até 12/11/2026 (vencimento do boleto)"),
    (datetime(2026, 11, 10, 23, 59, 59), True, AGORA, "Pague até 23:59 (restam 13h 39min)"),  # boleto vencendo hoje
    (datetime(2026, 11, 11, 10, 20), True, AGORA, "Pague até 11/11/2026 (vencimento do boleto)"),  # exatamente 24h: só a data
    (datetime(2026, 11, 11, 10, 19), True, AGORA, "Pague até amanhã, 10:19 (restam 23h 59min)"),
])
def test_texto_do_prazo(prazo, boleto, agora, esperado):
    assert servico.texto_prazo(prazo, boleto, agora) == esperado


def test_prazo_do_plano_pix_boleto_e_sem_cobranca():
    inicio = date(2026, 11, 23)
    assert servico.prazo_do_plano(_resumo(CRIADO_10H, inicio, FormaPagamento.PIX), AGORA) == datetime(2026, 11, 10, 14, 0)
    assert servico.prazo_do_plano(_resumo(CRIADO_10H, inicio), AGORA) == datetime(2026, 11, 10, 14, 0)  # prazo_previsto
    # boleto: menor entre 3 dias úteis (13/11) e o 2º dia útil antes da 1ª sessão (19/11), às 23:59:59
    boleto = servico.prazo_do_plano(_resumo(CRIADO_10H, inicio, FormaPagamento.BOLETO), AGORA)
    assert boleto.date() == date(2026, 11, 13) and boleto.time() > time(23, 59)


def test_pagamentos_ordenados_pelo_menor_prazo():
    boleto = _resumo(CRIADO_10H, date(2026, 11, 23), FormaPagamento.BOLETO, plano_id=1)
    pix_14h = _resumo(CRIADO_10H, date(2026, 11, 23), FormaPagamento.PIX, plano_id=2)
    sem_cobranca_13h = _resumo(CRIADO_9H, date(2026, 11, 23), plano_id=3)
    ativo = _resumo(CRIADO_9H, date(2026, 11, 23), situacao=inicio_service.ATIVO, plano_id=4)
    lista = servico.pagamentos_pendentes([boleto, pix_14h, ativo, sem_cobranca_13h], AGORA)
    assert [p.resumo.plano.id for p in lista] == [3, 2, 1]
    assert [p.texto for p in lista] == [
        "Pague até 13:00 (restam 2h 40min)", "Pague até 14:00 (restam 3h 40min)", "Pague até 13/11/2026 (vencimento do boleto)",
    ]


@pytest.mark.parametrize("data_hora, agora, esperado", [
    (datetime(2026, 11, 10, 16, 0), AGORA, "hoje"),
    (datetime(2026, 11, 11, 8, 0), datetime(2026, 11, 10, 23, 0), "amanhã"),  # 9h depois, mas outro dia
    (datetime(2026, 11, 17, 9, 0), AGORA, "em 7 dias"),
    (datetime(2026, 11, 12, 0, 30), datetime(2026, 11, 10, 23, 59), "em 2 dias"),  # dias de calendário, não 24h
    (datetime(2027, 1, 1, 9, 0), datetime(2026, 12, 31, 18, 0), "amanhã"),
])
def test_quando_em_dias_de_calendario(data_hora, agora, esperado):
    assert servico.quando_em_dias(data_hora, agora) == esperado


def test_data_por_extenso():
    assert servico.data_por_extenso(datetime(2026, 11, 17, 9, 0)) == "Terça, 17 de novembro, às 09:00"
    assert servico.data_por_extenso(datetime(2026, 3, 1, 14, 0)) == "Domingo, 1 de março, às 14:00"


@pytest.mark.parametrize("frequencia, dias, horario, esperado", [
    (1, [1], time(9), "Terças às 09:00, 1 vez por semana"),
    (2, [3, 1], time(15), "Terças e quintas às 15:00, 2 vezes por semana"),
    (3, [0, 2, 4], time(8), "Segundas, quartas e sextas às 08:00, 3 vezes por semana"),
])
def test_configuracao_curta(frequencia, dias, horario, esperado):
    assert servico.configuracao_curta(frequencia, dias, horario) == esperado


# --- Estados da home -----------------------------------------------------------------------------------

def test_recem_cadastrado_so_ve_os_primeiros_passos(client, maria):
    main = _main(_html(client.get("/")))
    assert "Primeiros passos" in main and 'href="/pacientes/novo"' in main
    assert 'href="/especialidades"' not in main  # sem atendido: nenhum "Contratar plano"
    for ausente in ("Próxima sessão", "Planos de atendimento", "aguardando pagamento", "Nenhuma sessão agendada"):
        assert ausente not in main, ausente


def test_atendido_sem_plano_tem_o_botao_do_topo_e_mais_nada(client, db, maria):
    criar_paciente(db, maria.id)
    main = _main(_html(client.get("/")))
    assert main.count(">Contratar plano</a>") == 1
    acoes = re.search(r'<div class="pagina-acoes">.*?</div>', main).group(0)
    assert 'href="/especialidades" class="btn btn-primary"' in acoes
    assert 'aria-current="step"> <span class="passo-marcador" aria-hidden="true">2</span>' in main
    for ausente in ("Próxima sessão", "Planos de atendimento", "Pagar agora"):
        assert ausente not in main, ausente


def test_aguardando_um_plano(client, db, carla, maria):
    pedro = criar_paciente(db, maria.id, nome="Pedro Souza")
    plano = criar_plano(db, pedro, carla, [(datetime(2026, 11, 20, 9, 0), PENDENTE)], criado_em=CRIADO_10H)
    main = _main(_html(client.get("/")))
    bloco = re.search(r'<section class="ini-pagar".*?</section>', main).group(0)
    assert '<h2 class="ini-pagar-titulo" id="titulo-pagar">Plano aguardando pagamento</h2>' in bloco
    assert "<strong>Pedro Souza, Psicopedagogia</strong>" in bloco
    assert "Pague até 14:00 (restam 3h 40min)." in bloco
    assert f'href="/planos/{plano.id}/pagamento"' in bloco
    assert "Primeiros passos" in main
    assert "Próxima sessão" not in main and "Planos de atendimento" not in main  # sem blocos vazios
    assert main.count("Pagar agora") == 1


def test_aguardando_dois_planos_menor_prazo_primeiro(client, db, carla, rafael, maria):
    pedro = criar_paciente(db, maria.id, nome="Pedro Souza")
    ana = criar_paciente(db, maria.id, nome="Ana Souza")
    boleto = criar_plano(db, ana, rafael, [(datetime(2026, 11, 23, 16, 0), PENDENTE)], criado_em=CRIADO_10H)
    _pagamento(db, boleto, FormaPagamento.BOLETO)
    pix = criar_plano(db, pedro, carla, [(datetime(2026, 11, 20, 9, 0), PENDENTE)], criado_em=CRIADO_10H)
    main = _main(_html(client.get("/")))
    bloco = re.search(r'<section class="ini-pagar".*?</section>', main).group(0)
    assert "Planos aguardando pagamento</h2>" in bloco
    assert bloco.index(f"/planos/{pix.id}/pagamento") < bloco.index(f"/planos/{boleto.id}/pagamento")
    assert "Pague até 13/11/2026 (vencimento do boleto)." in bloco
    assert main.count("Pagar agora") == 2 == bloco.count("Pagar agora")


def _com_planos(db, maria, carla, rafael):
    pedro = criar_paciente(db, maria.id, nome="Pedro Souza")
    ana = criar_paciente(db, maria.id, nome="Ana Souza")
    ativo = criar_plano(db, pedro, carla, [
        (datetime(2026, 10, 13, 9, 0), REALIZADA), (datetime(2026, 10, 20, 9, 0), REALIZADA),
        (datetime(2026, 11, 3, 9, 0), REALIZADA), (datetime(2026, 11, 10, 9, 0), REALIZADA),
        (datetime(2026, 11, 17, 9, 0), AGENDADA), (datetime(2026, 11, 24, 9, 0), AGENDADA),
        (datetime(2026, 12, 1, 9, 0), AGENDADA), (datetime(2026, 12, 8, 9, 0), AGENDADA),
    ], valor_total="1116.00")
    ativo.desconto_percentual = 7
    aguardando = criar_plano(db, ana, rafael, [(datetime(2026, 11, 16, 16, 0), PENDENTE)], criado_em=CRIADO_10H, valor_total="1088.10")
    _pagamento(db, aguardando, FormaPagamento.BOLETO)
    encerrado = criar_plano(db, ana, rafael, [(datetime(2026, 9, 14, 14, 0), REALIZADA)], valor_total="494.00")
    encerrado_2 = criar_plano(db, pedro, carla, [(datetime(2026, 8, 4, 9, 0), REALIZADA)], valor_total="300.00")
    db.commit()
    return ativo, aguardando, encerrado, encerrado_2


def test_com_planos_proxima_sessao_lista_e_encerrados(client, db, carla, rafael, maria):
    ativo, aguardando, encerrado, encerrado_2 = _com_planos(db, maria, carla, rafael)
    main = _main(_html(client.get("/")))
    assert "Primeiros passos" not in main
    assert main.count(">Contratar plano</a>") == 1

    proxima = re.search(r'<section class="cartao ini-proxima esp-psicopedagogia".*?</section>', main).group(0)
    assert "Próxima sessão, em 7 dias" in proxima and "Terça, 17 de novembro, às 09:00" in proxima
    assert '<strong>Pedro Souza</strong> <span class="chip-especialidade">Psicopedagogia</span> <span>com Carla Mendes</span>' in proxima

    lista = main[main.index('id="titulo-planos"'):]
    assert lista.index(">Ana Souza</h3>") < lista.index(">Pedro Souza</h3>")  # atendidos em ordem alfabética
    cartao_ativo = re.search(rf'<article class="ini-plano esp-psicopedagogia".*?/planos/{ativo.id}".*?</article>', lista).group(0)
    assert '<span class="badge rounded-pill status-ativo">Ativo</span>' in cartao_ativo
    assert "Terças às 09:00, 1 vez por semana" in cartao_ativo and "13/10/2026 a 08/12/2026" in cartao_ativo
    assert "4 de 8 sessões realizadas" in cartao_ativo and "width: 50.0%" in cartao_ativo
    assert "R$ 1.116,00</strong> <span>com 7% de desconto</span>" in cartao_ativo

    cartao_aguardando = re.search(rf'<article class="ini-plano esp-fonoaudiologia"(?:(?!</article>).)*/planos/{aguardando.id}".*?</article>', lista).group(0)
    assert "Aguardando pagamento</span>" in cartao_aguardando and "Pagar agora" not in cartao_aguardando
    assert "de desconto" not in cartao_aguardando  # sem desconto: nenhuma linha

    detalhes = re.search(r'<details class="ini-encerrados">.*?</details>', lista).group(0)
    assert "<summary>2 planos encerrados</summary>" in detalhes and " open" not in detalhes.split(">")[0]
    for plano in (encerrado, encerrado_2):
        assert f'href="/planos/{plano.id}/renovar"' in detalhes and f'href="/planos/{plano.id}"' in detalhes
    assert lista.count("Renovar") == 2 == detalhes.count("Renovar")
    assert lista.count("<details") == 1  # um único bloco de encerrados
    assert main.count("Pagar agora") == 1  # só o bloco âmbar
    assert main.index('class="ini-pagar"') < main.index('class="cartao ini-proxima')


def test_um_plano_encerrado_no_singular(client, db, carla, maria):
    pedro = criar_paciente(db, maria.id)
    criar_plano(db, pedro, carla, [(datetime(2026, 11, 17, 9, 0), AGENDADA)])
    criar_plano(db, pedro, carla, [(datetime(2026, 8, 4, 9, 0), REALIZADA)])
    assert "<summary>1 plano encerrado</summary>" in _html(client.get("/"))


def test_so_encerrados_sem_proxima_sessao(client, db, carla, maria):
    criar_plano(db, criar_paciente(db, maria.id), carla, [(datetime(2026, 8, 4, 9, 0), REALIZADA)])
    main = _main(_html(client.get("/")))
    assert "Nenhuma sessão agendada" in main and "Remarcar" not in main
    assert "<summary>1 plano encerrado</summary>" in main


@pytest.mark.parametrize("sessao, remarcar", [
    (AGORA + timedelta(hours=4), True),  # RN02: exatamente 4h ainda pode
    (AGORA + timedelta(hours=3, minutes=59), False),
    (AGORA + timedelta(days=2), True),
])
def test_remarcar_some_com_menos_de_4h(client, db, carla, maria, sessao, remarcar):
    pedro = criar_paciente(db, maria.id)
    plano = criar_plano(db, pedro, carla, [(sessao, AGENDADA)])
    sessao_id = db.query(Sessao).filter_by(plano_id=plano.id).one().id
    proxima = re.search(r'<section class="cartao ini-proxima.*?</section>', _html(client.get("/"))).group(0)
    assert (f'href="/consultas/{sessao_id}/remarcar"' in proxima) is remarcar
    assert '<a href="/consultas" class="btn btn-outline-primary btn-sm">Ver consultas</a>' in proxima


def test_proxima_sessao_hoje(client, db, carla, maria):
    criar_plano(db, criar_paciente(db, maria.id), carla, [(datetime(2026, 11, 10, 16, 0), AGENDADA)])
    assert "Próxima sessão, hoje" in _html(client.get("/"))


def test_especialidade_sem_cor_propria_usa_tom_neutro(client, db, maria):
    terapia = criar_funcionario(db, criar_especialidade(db, "Terapia Ocupacional", "160.00"), email="t@exemplo.com")
    criar_plano(db, criar_paciente(db, maria.id), terapia, [(datetime(2026, 11, 17, 9, 0), AGENDADA)])
    main = _main(_html(client.get("/")))
    assert '<section class="cartao ini-proxima esp-neutra"' in main and '<article class="ini-plano esp-neutra"' in main


def test_so_dados_do_proprio_cliente(client, db, carla, rafael, maria):
    _com_planos(db, maria, carla, rafael)
    outro = criar_cliente(db, email="outro@exemplo.com", nome="Outra Pessoa")
    alheio = criar_paciente(db, outro.id, nome="Paciente Alheio")
    criar_plano(db, alheio, carla, [(datetime(2026, 11, 11, 9, 0), AGENDADA)])
    criar_plano(db, alheio, rafael, [(datetime(2026, 11, 20, 9, 0), PENDENTE)], criado_em=CRIADO_9H)
    main = _main(_html(client.get("/")))
    assert "Paciente Alheio" not in main and "Outra Pessoa" not in main
    assert "Próxima sessão, em 7 dias" in main  # a sessão alheia de amanhã não entra
    assert main.count("Pagar agora") == 1


def test_sem_javascript_novo(client, db, carla, rafael, maria):
    _com_planos(db, maria, carla, rafael)
    assert _html(client.get("/")).count("<script") == 1  # só o bundle do Bootstrap


# --- SQL -----------------------------------------------------------------------------------------------

def _contar_sql(client, db) -> int:
    comandos = []
    ouvinte = lambda *args: comandos.append(args[2])  # noqa: E731
    event.listen(db.get_bind(), "before_cursor_execute", ouvinte)
    try:
        assert client.get("/").status_code == 200
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", ouvinte)
    return len(comandos)


def test_mesmo_numero_de_consultas_com_1_e_com_20_planos(client, db, carla, rafael, maria):
    pedro = criar_paciente(db, maria.id, nome="Pedro Souza")
    criar_plano(db, pedro, carla, [(datetime(2026, 11, 17, 9, 0), AGENDADA)])
    _contar_sql(client, db)  # aquece: a 1ª requisição recarrega o usuário logado
    um = _contar_sql(client, db)
    for i in range(19):
        atendido = criar_paciente(db, maria.id, nome=f"Atendido {i:02d}")
        if i % 4 == 0:
            plano = criar_plano(db, atendido, rafael, [(datetime(2026, 11, 25, 9, 0), PENDENTE)], criado_em=CRIADO_10H)
            if i % 8 == 0:
                _pagamento(db, plano, FormaPagamento.BOLETO)
        elif i % 4 == 1:
            criar_plano(db, atendido, rafael, [(datetime(2026, 8, 4 + i, 9, 0), REALIZADA)])
        else:
            criar_plano(db, atendido, carla, [(datetime(2026, 10, 6, 9, 0), REALIZADA), (datetime(2026, 11, 18 + i % 3, 9, 0), AGENDADA)])
    db.expire_all()
    _contar_sql(client, db)
    vinte = _contar_sql(client, db)
    assert vinte == um
