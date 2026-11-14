import re
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import event, select

from app.core.templating import resumo_corrija
from app.models.avaliacao import Avaliacao
from app.models.cliente import Cliente
from app.models.historico_remarcacao import HistoricoRemarcacao
from app.models.pagamento import FormaPagamento, Pagamento, StatusPagamento
from app.models.sessao import Sessao, StatusSessao
from app.routers import cliente as cliente_router
from app.routers import consultas as consultas_router
from app.routers import historico as historico_router
from app.services import cliente_service, consultas_service
from app.services.contratacao_service import texto_prazo_pagamento
from tests.fabricas import (
    criar_admin, criar_cliente, criar_especialidade, criar_funcionario, criar_paciente, criar_plano, logar,
)

AGENDADA = StatusSessao.AGENDADA
PENDENTE = StatusSessao.PENDENTE_PAGAMENTO
REALIZADA = StatusSessao.REALIZADA
AGORA = datetime(2026, 11, 10, 10, 0)  # terça, horário local
AGORA_UTC = datetime(2026, 11, 10, 13, 0)
CRIADO_PAGO = datetime(2026, 10, 1, 12, 0)
CRIADO_9H = datetime(2026, 11, 10, 12, 0)  # UTC = 09:00 local; Pix até 13:00
CRIADO_8H = datetime(2026, 11, 10, 11, 0)  # UTC = 08:00 local; Pix até 12:00
CRIADO_ONTEM = datetime(2026, 11, 9, 12, 0)  # prazo do Pix venceu ontem


@pytest.fixture(autouse=True)
def relogio_fixo(monkeypatch):
    for modulo in (consultas_router, historico_router):
        monkeypatch.setattr(modulo, "agora_local", lambda: AGORA)
    monkeypatch.setattr(consultas_router, "agora_utc", lambda: AGORA_UTC)


@pytest.fixture
def fono(db):
    return criar_funcionario(db, criar_especialidade(db, "Fonoaudiologia"))


@pytest.fixture
def cliente(db):
    return criar_cliente(db)


@pytest.fixture
def pedro(db, cliente):
    return criar_paciente(db, cliente.id, nome="Pedro Souza", nascimento=date(2019, 3, 1))


@pytest.fixture
def logado(client, cliente):
    logar(client, cliente.email)
    return client


def _html(resposta, codigo=200) -> str:
    assert resposta.status_code == codigo, resposta.status_code
    return " ".join(resposta.text.split())


def _sessao(db, quando) -> Sessao:
    return db.scalar(select(Sessao).where(Sessao.data_hora == quando))


# --- Parte A: Consultas e Remarcar ------------------------------------------------------------------------

def test_proximas_vazia_com_anteriores(db, logado, pedro, fono):
    criar_plano(db, pedro, fono, [(datetime(2026, 11, 3, 9, 0), REALIZADA)], criado_em=CRIADO_PAGO)
    vazio = re.search(r'id="consultas-vazio">.*?</section>', _html(logado.get("/consultas"))).group(0)
    assert "Nenhuma consulta próxima." in vazio and "aba=anteriores" in vazio


@pytest.mark.parametrize("com_anterior, esperado", [
    (False, "Nenhuma consulta confirmada ainda. As sessões abaixo aguardam o pagamento do plano."),
    (True, "Nenhuma consulta próxima. As sessões abaixo aguardam o pagamento do plano."),
])
def test_frase_com_bloco_ambar(db, logado, pedro, fono, com_anterior, esperado):
    if com_anterior:
        criar_plano(db, pedro, fono, [(datetime(2026, 11, 3, 9, 0), REALIZADA)], criado_em=CRIADO_PAGO)
    criar_plano(db, pedro, fono, [(datetime(2026, 11, 12, 9, 0), PENDENTE)], criado_em=CRIADO_9H)
    html = _html(logado.get("/consultas"))
    assert f'<p class="cons-nota" id="sem-confirmadas">{esperado}</p>' in html
    assert "Elas ficam confirmadas e podem ser remarcadas depois que o pagamento do plano for recebido." in html


@pytest.fixture
def consulta(db, pedro, fono):
    criar_plano(db, pedro, fono, [(datetime(2026, 11, 17, 9, 0), AGENDADA)], criado_em=CRIADO_PAGO)
    return _sessao(db, datetime(2026, 11, 17, 9, 0))


def _ocupar(db, fono, quando):
    outro = criar_cliente(db, email="outro@exemplo.com")
    criar_plano(db, criar_paciente(db, outro.id, nome="Outro"), fono, [(quando, AGENDADA)], criado_em=CRIADO_PAGO)


def _marcados(html: str) -> list[str]:
    return re.findall(r'<input type="radio" class="btn-check" name="novo_horario"[^>]*value="([^"]+)"[^>]*\bchecked\b', html)


def test_409_nao_devolve_horario_ocupado_marcado(db, logado, consulta, fono):
    _ocupar(db, fono, datetime(2026, 11, 19, 10, 0))
    html = _html(logado.post(f"/consultas/{consulta.id}/remarcar", data={"novo_horario": "2026-11-19T10:00"}), 409)
    assert _marcados(html) == []
    assert re.search(r'id="h-10" value="2026-11-19T10:00"[^>]*disabled', html)


def test_erro_com_horario_ainda_livre_volta_marcado(db, logado, consulta, monkeypatch):
    def recusar(*args, **kwargs):
        raise consultas_router.remarcacao_service.RemarcacaoInvalida("Erro de teste.")
    monkeypatch.setattr(consultas_router.remarcacao_service, "remarcar", recusar)
    html = _html(logado.post(f"/consultas/{consulta.id}/remarcar", data={"novo_horario": "2026-11-19T10:00"}), 400)
    assert _marcados(html) == ["2026-11-19T10:00"]


def test_frase_e_botao_so_com_horario_livre_marcado():
    css = (Path(__file__).resolve().parent.parent / "app" / "static" / "css" / "styles.css").read_text(encoding="utf-8")
    regra = '#form-remarcar:not(:has(input[name="novo_horario"]:checked:not(:disabled)))'
    assert f"{regra} .remarcar-troca," in css and f"{regra} #btn-confirmar {{" in css


@pytest.mark.parametrize("valor, codigo", [
    ("", 400), ("abc", 400), ("²", 400), ("2026-11-19T12:00", 400), ("2026-11-19T09:30", 400),
    ("9999-99-99T99:99", 400), ("2026-11-19T10:00", 409),
])
def test_post_invalido_responde_amigavel_e_nada_muda(db, logado, consulta, fono, valor, codigo):
    _ocupar(db, fono, datetime(2026, 11, 19, 10, 0))
    resposta = logado.post(f"/consultas/{consulta.id}/remarcar", data={"novo_horario": valor})
    assert resposta.status_code == codigo
    html = _html(resposta, codigo)
    assert 'role="alert"' in html and "Traceback" not in html and "Internal Server Error" not in html
    db.expire_all()
    assert _sessao(db, datetime(2026, 11, 17, 9, 0)).foi_remarcada is False
    assert db.scalar(select(HistoricoRemarcacao.id)) is None


# --- Parte B: Histórico -----------------------------------------------------------------------------------

def test_pilulas_com_idade_e_sem_todos(db, logado, cliente, pedro, fono):
    criar_paciente(db, cliente.id, nome="Ana Souza", nascimento=date(2017, 12, 1))
    criar_plano(db, pedro, fono, [(datetime(2026, 11, 3, 9, 0), REALIZADA)], criado_em=CRIADO_PAGO)
    html = _html(logado.get("/historico", params={"atendido": str(pedro.id)}))
    pilulas = re.search(r'<nav class="hist-atendidos.*?</nav>', html).group(0)
    assert 'Ana Souza <span class="hist-idade">· 8 anos</span>' in pilulas
    assert 'Pedro Souza <span class="hist-idade">· 7 anos</span>' in pilulas
    assert "Todos" not in pilulas and "2017" not in html and "2019" not in html


@pytest.mark.parametrize("valor", ["abc", "²", "-1", "999", "99999999999999999999"])
def test_atendido_invalido_responde_404(db, logado, pedro, valor):
    assert logado.get("/historico", params={"atendido": valor}).status_code == 404


def test_resumo_com_avaliacoes_em_destaque(db, logado, pedro, fono):
    criar_plano(db, pedro, fono, [(datetime(2026, 11, 3, 9, 0), REALIZADA)], criado_em=CRIADO_PAGO)
    db.add(Avaliacao(sessao_id=_sessao(db, datetime(2026, 11, 3, 9, 0)).id, arquivo_url="a.pdf", comparecimento=True))
    db.commit()
    html = _html(logado.get("/historico"))
    assert ('<li class="hist-resumo-destaque"><strong class="font-monospace-num">1</strong> <span>avaliação disponível'
            "</span></li>") in html
    assert "1</strong> <span>consulta realizada</span>" in html


def test_linha_ambar_leva_ao_plano_de_menor_prazo(db, logado, pedro, fono):
    criar_plano(db, pedro, fono, [(datetime(2026, 11, 3, 9, 0), REALIZADA)], criado_em=CRIADO_PAGO)
    tarde = criar_plano(db, pedro, fono, [(datetime(2026, 11, d, 9, 0), PENDENTE) for d in (19, 26)], criado_em=CRIADO_9H)
    cedo = criar_plano(db, pedro, fono, [(datetime(2026, 11, 18, 14, 0), PENDENTE)], criado_em=CRIADO_8H)
    criar_plano(db, pedro, fono, [(datetime(2026, 11, 20, 15, 0), PENDENTE)], criado_em=CRIADO_ONTEM)  # vencido
    html = _html(logado.get("/historico"))
    linha = re.search(r'<div class="ini-pagar hist-pagar" role="note" id="historico-pagar">.*?</div>', html).group(0)
    texto = texto_prazo_pagamento(cedo.criado_em, cedo.data_inicio, None, AGORA).linha
    assert texto == "Gere a cobrança até hoje, às 12:00 (restam 2h)"
    assert f"<strong>3 sessões aguardando pagamento.</strong> {texto}." in linha
    assert f'href="/planos/{cedo.id}/pagamento"' in linha and f"/planos/{tarde.id}/" not in linha
    assert html.index('id="historico-pagar"') < html.index('class="hist-lista"')


def test_sem_pendente_a_linha_nao_aparece(db, logado, pedro, fono):
    criar_plano(db, pedro, fono, [(datetime(2026, 11, 3, 9, 0), REALIZADA)], criado_em=CRIADO_PAGO)
    assert "historico-pagar" not in _html(logado.get("/historico"))


def test_pendencia_faz_uma_unica_consulta(db, cliente, pedro, fono):
    plano = criar_plano(db, pedro, fono, [(datetime(2026, 11, 18, 14, 0), PENDENTE)], criado_em=CRIADO_9H)
    db.add(Pagamento(plano_id=plano.id, forma_pagamento=FormaPagamento.PIX, status=StatusPagamento.PENDENTE))
    db.commit()
    cliente_id, pedro_id = cliente.id, pedro.id
    comandos = []
    ouvinte = lambda *args: comandos.append(args[2])  # noqa: E731
    event.listen(db.get_bind(), "before_cursor_execute", ouvinte)
    try:
        pendencia = consultas_service.pendencia_do_atendido(db, cliente_id, pedro_id, AGORA)
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", ouvinte)
    assert len(comandos) == 1 and "paciente.cliente_id" in comandos[0]
    assert pendencia.prazo.linha == "Pague até hoje, às 13:00 (restam 3h)" and pendencia.sessoes == 1


def test_pendencia_de_outro_cliente_nao_aparece(db, cliente, pedro, fono):
    criar_plano(db, pedro, fono, [(datetime(2026, 11, 18, 14, 0), PENDENTE)], criado_em=CRIADO_9H)
    outro = criar_cliente(db, email="outro@exemplo.com")
    assert consultas_service.pendencia_do_atendido(db, outro.id, pedro.id, AGORA) is None


@pytest.mark.parametrize("futuras, total, pagina, na_pagina, esperado", [
    (0, 10, 1, 10, None),  # tudo no passado
    (10, 10, 1, 10, None),  # tudo no futuro
    (3, 10, 1, 10, 3),
    (20, 25, 1, 20, None),  # a transição cai no começo da página 2
    (20, 25, 2, 5, 0),
    (25, 43, 1, 20, None),
    (25, 43, 2, 20, 5),
    (25, 43, 3, 3, None),
])
def test_posicao_do_divisor_hoje(futuras, total, pagina, na_pagina, esperado):
    assert consultas_service.posicao_do_divisor(futuras, total, pagina, na_pagina) == esperado


def test_divisor_hoje_entre_futuras_e_passadas(db, logado, pedro, fono):
    criar_plano(db, pedro, fono, [(datetime(2026, 11, 17, 9, 0), AGENDADA), (datetime(2026, 11, 3, 9, 0), REALIZADA)],
                criado_em=CRIADO_PAGO)
    html = _html(logado.get("/historico"))
    assert html.count('id="divisor-hoje"') == 1 and "Hoje, 10/11/2026" in html
    assert html.index("17/11/2026") < html.index('id="divisor-hoje"') < html.index("03/11/2026")


def test_divisor_na_virada_de_pagina_aparece_uma_vez(db, logado, pedro, fono):
    futuras = [(datetime(2026, 11, 11, 8, 0) + timedelta(days=i), AGENDADA) for i in range(20)]
    passadas = [(datetime(2026, 10, 1, 8, 0) + timedelta(days=i), REALIZADA) for i in range(3)]
    criar_plano(db, pedro, fono, futuras + passadas, criado_em=CRIADO_PAGO)
    primeira = _html(logado.get("/historico"))
    segunda = _html(logado.get("/historico", params={"atendido": str(pedro.id), "pagina": "2"}))
    assert "divisor-hoje" not in primeira
    assert segunda.count('id="divisor-hoje"') == 1 and segunda.index('id="divisor-hoje"') < segunda.index('class="hist-linha')
    assert "Consultas 1 a 20 de 23" in primeira and "Consultas 21 a 23 de 23" in segunda


def test_linhas_com_borda_selos_e_avaliacao(db, logado, cliente, pedro, fono):
    musico = criar_funcionario(db, criar_especialidade(db, "Musicoterapia"), email="m@exemplo.com", nome="Mara")
    criar_plano(db, pedro, musico, [(datetime(2026, 10, 1, 9, 0), REALIZADA)], criado_em=CRIADO_PAGO)
    ok, falta, sem_registro = datetime(2026, 10, 20, 9, 0), datetime(2026, 10, 27, 9, 0), datetime(2026, 11, 9, 9, 0)
    criar_plano(db, pedro, fono, [(ok, REALIZADA), (falta, REALIZADA), (sem_registro, AGENDADA)], criado_em=CRIADO_PAGO)
    db.add_all([Avaliacao(sessao_id=_sessao(db, ok).id, arquivo_url="segredo-uuid.pdf", comparecimento=True,
                          liberado_em=datetime(2026, 10, 20, 15, 0)),
                Avaliacao(sessao_id=_sessao(db, falta).id, comparecimento=False)])
    db.get(Cliente, cliente.id).cpf = "529.982.247-25"
    db.commit()
    html = _html(logado.get("/historico"))
    assert '<li class="hist-linha esp-neutra">' in html and '<li class="hist-linha esp-fonoaudiologia">' in html
    assert 'status-nao-compareceu">Não compareceu</span>' in html
    assert 'status-pendente">Aguardando registro do profissional</span>' in html
    assert 'target="_blank" rel="noopener"' in html and "Liberada em" in html
    for privado in ("segredo-uuid", "529.982.247-25", "cliente@exemplo.com", "99999-1234", "dados/avaliacoes"):
        assert privado not in html.split("<main", 1)[1], privado


def test_vazio_sem_plano_contrata_para_o_atendido(db, logado, pedro):
    vazio = re.search(r'id="historico-vazio">.*?</section>', _html(logado.get("/historico"))).group(0)
    assert f'<a href="/especialidades?atendido={pedro.id}" class="btn btn-primary">Contratar plano para Pedro</a>' in vazio


def test_vazio_com_pendente_oferece_pagar(db, logado, pedro, monkeypatch):
    pendencia = consultas_service.PendenciaPagamento(77, 2, texto_prazo_pagamento(CRIADO_9H, date(2026, 11, 18), None, AGORA))
    monkeypatch.setattr(consultas_service, "pendencia_do_atendido", lambda *args: pendencia)
    vazio = re.search(r'id="historico-vazio">.*?</section>', _html(logado.get("/historico"))).group(0)
    assert '<a href="/planos/77/pagamento" class="btn btn-primary">Pagar agora</a>' in vazio


# --- Parte C: Meu perfil e Alterar senha ------------------------------------------------------------------

@pytest.mark.parametrize("telefone, cpf, esperado", [
    ("(54) 99999-1234", "529.982.247-25", None),
    ("(54) 99999-1234", None, "Falta o CPF para pagar um plano"),
    ("", "529.982.247-25", "Falta o telefone para pagar um plano"),
    (None, "  ", "Faltam o telefone e o CPF para pagar um plano"),
])
def test_texto_dos_dados_que_faltam(telefone, cpf, esperado):
    assert cliente_service.texto_dados_faltando(telefone, cpf) == esperado


@pytest.mark.parametrize("quantidade, esperado", [
    (1, "Corrija o campo indicado. Nada foi salvo."), (2, "Corrija os 2 campos indicados. Nada foi salvo."),
])
def test_resumo_corrija(quantidade, esperado):
    assert resumo_corrija(quantidade, "Nada foi salvo.") == esperado


def test_perfil_em_uma_coluna_com_faixa_do_cpf(db, logado, cliente):
    html = _html(logado.get("/perfil"))
    assert '<p class="ini-pagar-titulo">Falta o CPF para pagar um plano</p>' in html
    assert "Você já pode escolher e reservar um plano." in html
    assert 'id="titulo-seus-dados">Seus dados</h2>' in html and 'id="titulo-senha">Senha</h2>' in html
    assert "Ver atendidos" not in html and '<h2 class="cartao-titulo">Atendidos</h2>' not in html
    assert 'href="/conta/alterar-senha"' in html


def test_perfil_completo_sem_faixa(db, logado, cliente):
    db.get(Cliente, cliente.id).cpf = "529.982.247-25"
    db.commit()
    assert "perfil-faltando" not in _html(logado.get("/perfil"))


def test_campos_com_autocomplete_inputmode_e_cpf_sem_placeholder(logado):
    html = _html(logado.get("/perfil"))
    assert re.search(r'id="nome"[^>]*autocomplete="name"', html)
    assert re.search(r'id="email"[^>]*autocomplete="email"[^>]*inputmode="email"', html)
    assert re.search(r'id="telefone"[^>]*autocomplete="tel"[^>]*inputmode="tel"', html)
    cpf = re.search(r'<input type="text" id="cpf"[^>]*>', html).group(0)
    assert 'inputmode="numeric"' in cpf and "placeholder" not in cpf and 'autocomplete="off"' in cpf
    assert "Opcional agora, obrigatório para gerar a cobrança." in html


def test_perfil_com_erros_mostra_resumo_e_nada_salva(db, logado, cliente):
    resposta = logado.post("/perfil", data={"nome": "Maria", "email": "maria@exemplo.com", "telefone": "99999", "cpf": "123.456.789-00"})
    html = _html(resposta, 400)
    assert "Corrija os 2 campos indicados. Nada foi salvo." in html
    assert '<a href="#telefone">Telefone com DDD</a>' in html and '<a href="#cpf">CPF</a>' in html
    assert html.count('class="invalid-feedback"') == 2 and "perfil-faltando" not in html
    db.expire_all()
    assert db.get(Cliente, cliente.id).cpf is None


def test_perfil_salvo(db, logado):
    dados = {"nome": "Maria", "email": "maria@exemplo.com", "telefone": "(54) 99999-1234", "cpf": "529.982.247-25"}
    resposta = logado.post("/perfil", data=dados, follow_redirects=True)
    html = _html(resposta)
    assert "<span>Dados salvos.</span>" in html and "perfil-faltando" not in html


def test_faixa_nao_aparece_na_forma_de_pagamento(db, logado, pedro, fono, monkeypatch):
    monkeypatch.setattr(cliente_router, "agora_local", lambda: AGORA)
    plano = criar_plano(db, pedro, fono, [(datetime(2026, 11, 18, 14, 0), PENDENTE)], criado_em=CRIADO_9H)
    assert "perfil-faltando" not in _html(logado.get(f"/planos/{plano.id}/pagamento"))


@pytest.mark.parametrize("perfil", ["cliente", "funcionario", "admin"])
def test_alterar_senha_nos_tres_perfis(db, client, perfil):
    if perfil == "cliente":
        email = criar_cliente(db).email
    elif perfil == "funcionario":
        email = criar_funcionario(db, criar_especialidade(db)).usuario.email
    else:
        email = criar_admin(db).email
    logar(client, email)
    html = _html(client.get("/conta/alterar-senha"))
    assert '<a href="/" class="senha-cancelar" id="link-cancelar">Cancelar</a>' in html
    assert client.get("/").status_code == 200  # Cancelar leva à tela inicial de quem está logado
    erro = _html(client.post("/conta/alterar-senha", data={"senha_atual": "", "nova_senha": "abc", "confirmacao_senha": "x"}), 400)
    assert "Corrija os 2 campos indicados. A senha não foi alterada." in erro
    assert '<a href="#senha_atual">Senha atual</a>' in erro and '<a href="#nova_senha">Nova senha</a>' in erro
    assert ('Meu perfil' in html.split("<main", 1)[1]) == (perfil == "cliente")  # "voltar" só no Cliente
