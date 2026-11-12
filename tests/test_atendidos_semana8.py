import re
from collections import Counter
from datetime import date, datetime

import pytest
from sqlalchemy import event

from app.models.paciente import Paciente
from app.models.sessao import StatusSessao
from app.routers import cliente as cliente_router
from app.services import inicio_cliente_service as servico
from app.services import inicio_service
from app.services.inicio_service import AGUARDANDO_PAGAMENTO, ATIVO, ENCERRADO
from tests.fabricas import criar_cliente, criar_especialidade, criar_funcionario, criar_paciente, criar_plano, logar

AGENDADA = StatusSessao.AGENDADA
PENDENTE = StatusSessao.PENDENTE_PAGAMENTO
REALIZADA = StatusSessao.REALIZADA

AGORA = datetime(2026, 11, 10, 10, 0)  # horário local
CRIADO_NO_PRAZO = datetime(2026, 11, 10, 12, 0)  # UTC = 09:00 local: o Pix vence às 13:00
CRIADO_VENCIDO = datetime(2026, 11, 10, 8, 0)  # UTC = 05:00 local: o Pix venceu às 09:00


@pytest.fixture(autouse=True)
def relogio_fixo(monkeypatch):
    monkeypatch.setattr(cliente_router, "agora_local", lambda: AGORA)


@pytest.fixture
def carla(db):
    return criar_funcionario(db, criar_especialidade(db, "Psicopedagogia", "180.00"), email="carla@exemplo.com", nome="Carla Mendes")


@pytest.fixture
def maria(client, db):
    cliente = criar_cliente(db, email="maria@exemplo.com", nome="Maria Souza")
    logar(client, cliente.email)
    return cliente


def _html(resposta) -> str:
    return " ".join(resposta.text.split())


def _cartao(html: str, nome: str) -> str:
    cartoes = re.findall(r'<li class="cartao atd-cartao">.*?</li>', html)
    escolhidos = [c for c in cartoes if f">{nome}</h2>" in c]
    assert len(escolhidos) == 1, nome
    return escolhidos[0]


# --- Selos (puro) ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("contagem, esperado", [
    (Counter(), []),
    (Counter({ATIVO: 1}), [("1 plano ativo", "status-ativo")]),
    (Counter({ATIVO: 2}), [("2 planos ativos", "status-ativo")]),
    (Counter({AGUARDANDO_PAGAMENTO: 1}), [("1 aguardando pagamento", "status-pendente")]),
    (Counter({AGUARDANDO_PAGAMENTO: 3}), [("3 aguardando pagamento", "status-pendente")]),
    (Counter({ENCERRADO: 1}), [("1 plano encerrado", "status-inativo")]),
    (Counter({ENCERRADO: 2, ATIVO: 1, AGUARDANDO_PAGAMENTO: 1}), [
        ("1 plano ativo", "status-ativo"), ("1 aguardando pagamento", "status-pendente"), ("2 planos encerrados", "status-inativo"),
    ]),
])
def test_texto_dos_selos(contagem, esperado):
    assert servico.texto_selos(contagem) == esperado


# --- Lista -------------------------------------------------------------------------------------------------

def test_selos_por_atendido(client, db, carla, maria):
    pedro = criar_paciente(db, maria.id, nome="Pedro Souza")
    ana = criar_paciente(db, maria.id, nome="Ana Souza")
    laura = criar_paciente(db, maria.id, nome="Laura Souza")
    criar_plano(db, pedro, carla, [(datetime(2026, 11, 17, 9, 0), AGENDADA)])  # ativo
    criar_plano(db, pedro, carla, [(datetime(2026, 11, 20, 9, 0), PENDENTE)], criado_em=CRIADO_NO_PRAZO)  # aguardando
    criar_plano(db, ana, carla, [(datetime(2026, 11, 21, 9, 0), PENDENTE)], criado_em=CRIADO_NO_PRAZO)
    criar_plano(db, ana, carla, [(datetime(2026, 9, 1, 9, 0), REALIZADA)])  # encerrado
    criar_plano(db, ana, carla, [(datetime(2026, 9, 8, 9, 0), REALIZADA)])
    html = _html(client.get("/pacientes"))

    pedro_html = _cartao(html, "Pedro Souza")
    assert '<span class="badge rounded-pill status-ativo">1 plano ativo</span><span class="badge rounded-pill status-pendente">1 aguardando pagamento</span>' in pedro_html
    ana_html = _cartao(html, "Ana Souza")
    assert ">1 aguardando pagamento</span>" in ana_html and 'rounded-pill status-inativo">2 planos encerrados</span>' in ana_html
    assert "plano ativo" not in ana_html
    laura_html = _cartao(html, "Laura Souza")
    assert '<span class="atd-sem-plano">Ainda sem plano.</span>' in laura_html
    assert f'<a href="/especialidades?atendido={laura.id}" class="btn btn-primary btn-sm">Contratar plano<span class="visually-hidden"> para Laura Souza</span></a>' in laura_html
    assert "Contratar plano" not in pedro_html and "Contratar plano" not in ana_html


def test_selos_usam_a_mesma_classificacao_do_inicio(db, carla, maria):
    pedro = criar_paciente(db, maria.id, nome="Pedro Souza")
    criar_plano(db, pedro, carla, [(datetime(2026, 11, 10, 9, 0), REALIZADA)])  # última sessão hoje: ativo
    criar_plano(db, pedro, carla, [(datetime(2026, 11, 9, 9, 0), REALIZADA)])  # ontem: encerrado
    criar_plano(db, pedro, carla, [(datetime(2026, 11, 20, 9, 0), PENDENTE)], criado_em=CRIADO_NO_PRAZO)
    criar_plano(db, pedro, carla, [(datetime(2026, 11, 21, 9, 0), PENDENTE)], criado_em=CRIADO_VENCIDO)  # RN09: some
    contagem = servico.situacoes_por_atendido(db, maria.id, AGORA)[pedro.id]
    do_inicio = Counter(r.situacao for r in inicio_service.planos_do_cliente(db, maria.id, AGORA))
    assert contagem == do_inicio == Counter({ATIVO: 1, ENCERRADO: 1, AGUARDANDO_PAGAMENTO: 1})


def test_idade_e_nascimento_sem_flexao_de_genero(client, db, maria):
    criar_paciente(db, maria.id, nome="Ana Souza", nascimento=date(2016, 3, 12))
    criar_paciente(db, maria.id, nome="Pedro Souza", nascimento=date(2016, 11, 11))  # faz 10 anos amanhã
    criar_paciente(db, maria.id, nome="Laura Souza", nascimento=date(2025, 12, 1))
    html = _html(client.get("/pacientes"))
    assert '10 anos (nascimento: <span class="font-monospace-num">12/03/2016</span>)' in _cartao(html, "Ana Souza")
    assert "9 anos (nascimento:" in _cartao(html, "Pedro Souza")
    assert "menos de 1 ano (nascimento:" in _cartao(html, "Laura Souza")
    assert "nascida" not in html.lower() and "nascido" not in html.lower()


def test_estado_vazio(client, maria):
    html = _html(client.get("/pacientes"))
    assert "Nenhum atendido cadastrado" in html
    assert "Cadastre a pessoa que será atendida para poder contratar um plano para ela." in html
    main = re.search(r"<main.*?</main>", html).group(0)
    assert main.count('href="/pacientes/novo"') == 1 and "Novo atendido" not in main


def test_novo_atendido_no_topo_e_acoes_do_cartao(client, db, maria):
    pedro = criar_paciente(db, maria.id, nome="Pedro Souza")
    html = _html(client.get("/pacientes"))
    acoes = re.search(r'<div class="pagina-acoes">.*?</div>', html).group(0)
    assert 'href="/pacientes/novo"' in acoes and "Novo atendido" in acoes
    cartao = _cartao(html, "Pedro Souza")
    assert f'href="/historico?atendido={pedro.id}"' in cartao and f'href="/pacientes/{pedro.id}/editar"' in cartao


def test_lista_nao_mostra_atendido_alheio(client, db, carla, maria):
    outro = criar_cliente(db, email="outro@exemplo.com", nome="Outra Pessoa")
    alheio = criar_paciente(db, outro.id, nome="Filho do Outro")
    criar_plano(db, alheio, carla, [(datetime(2026, 11, 17, 9, 0), AGENDADA)])
    meu = criar_paciente(db, maria.id, nome="Pedro Souza")
    html = _html(client.get("/pacientes"))
    assert "Filho do Outro" not in html and f"atendido={alheio.id}" not in html
    assert "Ainda sem plano." in _cartao(html, "Pedro Souza")  # o plano alheio não conta para ninguém daqui
    assert f"atendido={meu.id}" in html


@pytest.mark.parametrize("ident", ["abc", "0", "-1", "²", "99999999999999999999", "9999"])
def test_editar_com_id_invalido_ou_inexistente_da_404(client, maria, ident):
    assert client.get(f"/pacientes/{ident}/editar").status_code == 404


def test_editar_atendido_alheio_da_404(client, db, maria):
    alheio = criar_paciente(db, criar_cliente(db, email="outro@exemplo.com").id, nome="Filho do Outro")
    assert client.get(f"/pacientes/{alheio.id}/editar").status_code == 404
    assert client.post(f"/pacientes/{alheio.id}/editar", data={"nome": "X Y", "data_nascimento": "2018-01-01"}).status_code == 404


# --- Formulário --------------------------------------------------------------------------------------------

def test_formulario_novo_tem_dica_nota_e_campo_de_data(client, maria):
    html = _html(client.get("/pacientes/novo"))
    assert 'type="date" id="data_nascimento"' in html and 'max="2026-11-10"' in html
    assert 'id="data_nascimento-ajuda">A idade é calculada a partir da data de nascimento.</p>' in html
    assert 'aria-describedby="data_nascimento-ajuda"' in html
    assert "O profissional vê só o nome e a idade, nunca a data de nascimento." in html
    assert "planos já contratados" not in html and "aria-invalid" not in html


def test_formulario_editar_avisa_sobre_o_nome(client, db, maria):
    pedro = criar_paciente(db, maria.id, nome="Pedro Souza", nascimento=date(2018, 5, 3))
    html = _html(client.get(f"/pacientes/{pedro.id}/editar"))
    assert "Mudar o nome vale também para os planos já contratados." in html
    assert "O profissional vê só o nome e a idade, nunca a data de nascimento." in html
    assert 'value="Pedro Souza"' in html and 'value="2018-05-03"' in html


def _resumo(html: str) -> str:
    return re.search(r'<div class="alert alert-danger prof-form-erros".*?</div>', html).group(0)


def test_novo_com_dois_erros(client, db, maria):
    resposta = client.post("/pacientes/novo", data={"nome": "", "data_nascimento": ""})
    assert resposta.status_code == 400
    html = _html(resposta)
    resumo = _resumo(html)
    assert "Revise 2 campos antes de cadastrar." in resumo
    assert resumo.index('href="#nome"') < resumo.index('href="#data_nascimento"')
    for campo in ("nome", "data_nascimento"):
        assert f'id="{campo}-erro"><i class="bi bi-exclamation-circle me-1" aria-hidden="true"></i>Este campo é obrigatório.</p>' in html
    assert html.count('aria-invalid="true"') == 2 and html.count("form-control is-invalid") == 2
    assert 'aria-describedby="data_nascimento-erro data_nascimento-ajuda"' in html
    assert db.query(Paciente).count() == 0


@pytest.mark.parametrize("data, mensagem", [
    ("2026-11-11", "A data de nascimento não pode estar no futuro."),
    ("1899-12-31", "Confira o ano da data de nascimento."),
])
def test_novo_com_um_erro_de_data_mantem_as_mensagens(client, db, maria, data, mensagem):
    html = _html(client.post("/pacientes/novo", data={"nome": "Pedro Souza", "data_nascimento": data}))
    assert "Revise 1 campo antes de cadastrar." in _resumo(html)
    assert f"{mensagem}</p>" in html and 'value="Pedro Souza"' in html
    assert html.count('aria-invalid="true"') == 1
    assert db.query(Paciente).count() == 0


def test_editar_com_erro_usa_salvar(client, db, maria):
    pedro = criar_paciente(db, maria.id, nome="Pedro Souza", nascimento=date(2018, 5, 3))
    html = _html(client.post(f"/pacientes/{pedro.id}/editar", data={"nome": "", "data_nascimento": "2018-05-03"}))
    assert "Revise 1 campo antes de salvar." in _resumo(html)
    assert "Mudar o nome vale também para os planos já contratados." in html
    db.refresh(pedro)
    assert pedro.nome == "Pedro Souza"
    html = _html(client.post(f"/pacientes/{pedro.id}/editar", data={"nome": "", "data_nascimento": ""}))
    assert "Revise 2 campos antes de salvar." in _resumo(html)


# --- SQL -------------------------------------------------------------------------------------------------

def _contar_sql(client, db) -> int:
    comandos = []
    ouvinte = lambda *args: comandos.append(args[2])  # noqa: E731
    event.listen(db.get_bind(), "before_cursor_execute", ouvinte)
    try:
        assert client.get("/pacientes").status_code == 200
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", ouvinte)
    return len(comandos)


def test_mesmo_numero_de_consultas_com_poucos_e_muitos_atendidos(client, db, carla, maria):
    pedro = criar_paciente(db, maria.id, nome="Pedro Souza")
    criar_plano(db, pedro, carla, [(datetime(2026, 11, 17, 9, 0), AGENDADA)])
    _contar_sql(client, db)  # aquece: a 1ª requisição recarrega o usuário logado
    poucos = _contar_sql(client, db)
    for i in range(15):
        atendido = criar_paciente(db, maria.id, nome=f"Atendido {i:02d}")
        for j in range(i % 3):
            criar_plano(db, atendido, carla, [(datetime(2026, 9, 1 + j, 9, 0), REALIZADA), (datetime(2026, 11, 18 + j, 9, 0), AGENDADA)])
        if i % 4 == 0:
            criar_plano(db, atendido, carla, [(datetime(2026, 11, 25, 9, 0), PENDENTE)], criado_em=CRIADO_NO_PRAZO)
    db.expire_all()
    _contar_sql(client, db)
    assert _contar_sql(client, db) == poucos == 2  # atendidos + planos agrupados
