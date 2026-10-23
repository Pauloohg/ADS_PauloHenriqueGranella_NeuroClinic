from datetime import date, datetime

import pytest
from sqlalchemy import event

from app.models.cliente import Cliente
from app.models.plano import StatusPlano
from app.models.sessao import StatusSessao
from app.routers import admin as admin_router
from app.services import admin_clientes_service as servico
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

AGORA = datetime(2026, 3, 10, 11, 0)  # horário local
CRIADO_NO_PRAZO = datetime(2026, 3, 10, 13, 0)  # UTC = 10:00 local; prazo até 14:00 local
CRIADO_VENCIDO = datetime(2026, 3, 10, 9, 0)  # UTC = 06:00 local; prazo venceu às 10:00 local


@pytest.fixture(autouse=True)
def relogio_fixo(monkeypatch):
    monkeypatch.setattr(admin_router, "agora_local", lambda: AGORA)


@pytest.fixture
def fono(db):
    return criar_funcionario(db, criar_especialidade(db, "Fonoaudiologia"))


@pytest.fixture
def admin_logado(client, db):
    logar(client, criar_admin(db).email)
    return client


def _html(resposta) -> str:
    return " ".join(resposta.text.split())


def _definir_cpf(db, usuario, cpf):
    db.get(Cliente, usuario.id).cpf = cpf
    db.commit()


# --- Service: regras puras -----------------------------------------------------------------------

@pytest.mark.parametrize("cpf, esperado", [
    ("123.456.789-25", "***.***.***-25"),
    ("12345678925", "***.***.***-25"),
    (None, None),
    ("", None),
])
def test_mascarar_cpf_mostra_so_os_dois_ultimos_digitos(cpf, esperado):
    assert servico.mascarar_cpf(cpf) == esperado


@pytest.mark.parametrize("nascimento, esperado", [
    (date(2018, 3, 10), 8),  # aniversário hoje
    (date(2018, 3, 11), 7),  # aniversário amanhã
    (date(2018, 3, 9), 8),
    (date(2025, 12, 1), 0),
])
def test_calcular_idade_usa_a_data_recebida(nascimento, esperado):
    assert servico.calcular_idade(nascimento, AGORA.date()) == esperado


@pytest.mark.parametrize("texto, esperado", [("", 1), ("2", 2), ("0", 1), ("-1", 1), ("abc", 1), (None, 1)])
def test_interpretar_pagina(texto, esperado):
    assert servico.interpretar_pagina(texto) == esperado


# --- Service: lista ------------------------------------------------------------------------------

def test_lista_paginada_por_nome_com_20_por_pagina(db):
    for i in range(45):
        criar_cliente(db, email=f"c{i:02d}@exemplo.com", nome=f"Cliente {44 - i:02d}")

    primeira = servico.listar(db, AGORA, "", 1)
    terceira = servico.listar(db, AGORA, "", 3)

    assert (primeira.total, primeira.total_paginas) == (45, 3)
    assert [c.nome for c in primeira.clientes] == [f"Cliente {i:02d}" for i in range(20)]
    assert [c.nome for c in terceira.clientes] == [f"Cliente {i:02d}" for i in range(40, 45)]


def test_pagina_alem_do_fim_vai_para_a_ultima(db):
    for i in range(21):
        criar_cliente(db, email=f"c{i}@exemplo.com", nome=f"Cliente {i:02d}")
    resultado = servico.listar(db, AGORA, "", 99)
    assert resultado.pagina == 2 and len(resultado.clientes) == 1


def test_busca_por_nome_sem_diferenciar_maiusculas(db):
    criar_cliente(db, email="maria@exemplo.com", nome="Maria Souza")
    criar_cliente(db, email="joao@exemplo.com", nome="João Lima")
    assert [c.nome for c in servico.listar(db, AGORA, "SOUZA").clientes] == ["Maria Souza"]


def test_busca_por_email(db):
    criar_cliente(db, email="maria@exemplo.com", nome="Maria Souza")
    criar_cliente(db, email="joao@empresa.com.br", nome="João Lima")
    assert [c.nome for c in servico.listar(db, AGORA, "Empresa.COM").clientes] == ["João Lima"]


def test_busca_trata_curinga_como_texto(db):
    criar_cliente(db, email="maria@exemplo.com", nome="Maria Souza")
    assert servico.listar(db, AGORA, "%").clientes == []


def test_lista_so_tem_clientes(db, fono):
    criar_admin(db)
    criar_cliente(db)
    assert [c.nome for c in servico.listar(db, AGORA).clientes] == ["Maria Souza"]


def test_lista_conta_pacientes_e_planos_ativos(db, fono):
    cliente = criar_cliente(db)
    pedro = criar_paciente(db, cliente.id, nome="Pedro")
    criar_paciente(db, cliente.id, nome="Ana")
    criar_plano(db, pedro, fono, [(datetime(2026, 3, 12, 9, 0), AGENDADA)])
    criar_plano(db, pedro, fono, [(datetime(2026, 3, 13, 9, 0), AGENDADA), (datetime(2026, 3, 3, 9, 0), REALIZADA)])
    criar_plano(db, pedro, fono, [(datetime(2026, 2, 3, 9, 0), REALIZADA)])  # data_fim no passado: encerrado
    criar_plano(db, pedro, fono, [(datetime(2026, 3, 19, 9, 0), PENDENTE)], criado_em=CRIADO_NO_PRAZO)
    criar_plano(db, pedro, fono, [(datetime(2026, 3, 20, 9, 0), PENDENTE)], criado_em=CRIADO_VENCIDO)
    criar_cliente(db, email="sem@exemplo.com", nome="Zé Sem Pacientes")

    linhas = {c.nome: c for c in servico.listar(db, AGORA).clientes}

    assert (linhas["Maria Souza"].pacientes, linhas["Maria Souza"].planos_ativos) == (2, 2)
    assert (linhas["Zé Sem Pacientes"].pacientes, linhas["Zé Sem Pacientes"].planos_ativos) == (0, 0)


def test_lista_usa_a_regra_derivada_de_plano_ativo(db, fono):
    cliente = criar_cliente(db)
    pedro = criar_paciente(db, cliente.id, nome="Pedro")
    criar_plano(db, pedro, fono, [(datetime(2026, 3, 10, 9, 0), REALIZADA)])  # última sessão hoje: ativo
    criar_plano(db, pedro, fono, [(datetime(2026, 3, 9, 9, 0), REALIZADA)], status_plano=StatusPlano.ATIVO)  # ontem: encerrado
    criar_plano(db, pedro, fono, [(datetime(2026, 3, 12, 9, 0), AGENDADA)], status_plano=StatusPlano.ENCERRADO)  # ativo

    assert servico.listar(db, AGORA).clientes[0].planos_ativos == 2
    assert servico.listar(db, datetime(2026, 3, 11, 0, 0)).clientes[0].planos_ativos == 1


def test_lista_nao_consulta_o_banco_cliente_por_cliente(db, fono):
    for i in range(5):
        cliente = criar_cliente(db, email=f"c{i}@exemplo.com", nome=f"Cliente {i}")
        criar_plano(db, criar_paciente(db, cliente.id), fono, [(datetime(2026, 3, 12, 9, 0), AGENDADA)])
    consultas = []

    def registrar(conn, cursor, sql, *args):
        consultas.append(sql)

    event.listen(db.bind, "before_cursor_execute", registrar)
    try:
        resultado = servico.listar(db, AGORA)
    finally:
        event.remove(db.bind, "before_cursor_execute", registrar)
    assert len(resultado.clientes) == 5
    assert len(consultas) == 2  # total + página, independente do número de clientes


def test_data_de_cadastro_em_horario_local(db):
    usuario = criar_cliente(db)
    usuario.criado_em = datetime(2026, 3, 1, 2, 30)  # UTC = 28/02 23:30 local
    db.commit()
    assert servico.listar(db, AGORA).clientes[0].cadastrado_em == datetime(2026, 2, 28, 23, 30)


# --- Service: detalhe ----------------------------------------------------------------------------

def test_detalhe_com_pacientes_planos_e_cpf_mascarado(db, fono):
    cliente = criar_cliente(db)
    _definir_cpf(db, cliente, "529.982.247-25")
    pedro = criar_paciente(db, cliente.id, nome="Pedro", nascimento=date(2018, 5, 3))
    criar_paciente(db, cliente.id, nome="Ana", nascimento=date(2020, 3, 10))
    plano = criar_plano(db, pedro, fono, [(datetime(2026, 3, 12, 9, 0), AGENDADA)], valor_total="1620.00")

    detalhe = servico.detalhar(db, cliente.id, AGORA)

    assert detalhe.cpf_mascarado == "***.***.***-25"
    assert [(p.nome, p.idade) for p in detalhe.pacientes] == [("Ana", 6), ("Pedro", 7)]
    resumo = detalhe.pacientes[1].planos[0]
    assert (resumo.plano.id, resumo.especialidade, resumo.profissional, resumo.situacao) == (plano.id, "Fonoaudiologia", "Carlos Fono", "ativo")
    assert detalhe.pacientes[0].planos == []


def test_detalhe_mostra_encerrado_pela_data_fim(db, fono):
    cliente = criar_cliente(db)
    pedro = criar_paciente(db, cliente.id)
    encerrado = criar_plano(db, pedro, fono, [(datetime(2026, 3, 9, 9, 0), REALIZADA)])
    ativo = criar_plano(db, pedro, fono, [(datetime(2026, 3, 12, 9, 0), AGENDADA)], status_plano=StatusPlano.ENCERRADO)

    planos = {r.plano.id: r.situacao for r in servico.detalhar(db, cliente.id, AGORA).pacientes[0].planos}
    assert planos == {encerrado.id: "encerrado", ativo.id: "ativo"}


def test_detalhe_omite_plano_com_pagamento_vencido(db, fono):
    cliente = criar_cliente(db)
    pedro = criar_paciente(db, cliente.id)
    no_prazo = criar_plano(db, pedro, fono, [(datetime(2026, 3, 19, 9, 0), PENDENTE)], criado_em=CRIADO_NO_PRAZO)
    criar_plano(db, pedro, fono, [(datetime(2026, 3, 20, 9, 0), PENDENTE)], criado_em=CRIADO_VENCIDO)

    planos = servico.detalhar(db, cliente.id, AGORA).pacientes[0].planos
    assert [(r.plano.id, r.situacao) for r in planos] == [(no_prazo.id, "aguardando_pagamento")]


def test_detalhe_nao_mistura_pacientes_de_outro_cliente(db, fono):
    cliente = criar_cliente(db)
    outro = criar_cliente(db, email="outro@exemplo.com", nome="Outra Pessoa")
    criar_plano(db, criar_paciente(db, outro.id, nome="Alheio"), fono, [(datetime(2026, 3, 12, 9, 0), AGENDADA)])
    assert servico.detalhar(db, cliente.id, AGORA).pacientes == []


def test_detalhe_de_id_inexistente_ou_nao_cliente(db, fono):
    admin = criar_admin(db)
    for id_ in (9999, admin.id, fono.usuario.id):
        with pytest.raises(servico.ClienteNaoEncontrado):
            servico.detalhar(db, id_, AGORA)


# --- Rotas ---------------------------------------------------------------------------------------

def test_tela_da_lista(admin_logado, db, fono):
    cliente = criar_cliente(db)
    _definir_cpf(db, cliente, "529.982.247-25")
    criar_plano(db, criar_paciente(db, cliente.id), fono, [(datetime(2026, 3, 12, 9, 0), AGENDADA)])

    resposta = admin_logado.get("/admin/clientes")
    html = _html(resposta)

    assert resposta.status_code == 200
    assert f'href="/admin/clientes/{cliente.id}"' in html
    assert "Maria Souza" in html and "cliente@exemplo.com" in html and "(54) 99999-1234" in html
    assert "10/03/2026" in html
    assert "529" not in html and "-25" not in html  # CPF não aparece na lista
    for texto in ["Editar", "Excluir", "Desativar"]:
        assert texto not in html


def test_tela_da_lista_com_paginacao_preserva_a_busca(admin_logado, db):
    for i in range(25):
        criar_cliente(db, email=f"souza{i}@exemplo.com", nome=f"Souza {i:02d}")

    html = _html(admin_logado.get("/admin/clientes", params={"q": "souza"}))
    assert "Página 1 de 2" in html
    assert 'href="/admin/clientes?q=souza&amp;pagina=2"' in html

    html = _html(admin_logado.get("/admin/clientes", params={"q": "souza", "pagina": "2"}))
    assert "Souza 24" in html and "Souza 00" not in html


def test_tela_da_lista_pagina_invalida_nao_da_erro(admin_logado, db):
    criar_cliente(db)
    assert admin_logado.get("/admin/clientes?pagina=abc").status_code == 200


def test_tela_da_lista_busca_sem_resultado(admin_logado, db):
    criar_cliente(db)
    html = _html(admin_logado.get("/admin/clientes?q=inexistente"))
    assert "Nenhum cliente encontrado" in html


def test_tela_de_detalhe(admin_logado, db, fono):
    cliente = criar_cliente(db)
    _definir_cpf(db, cliente, "529.982.247-25")
    pedro = criar_paciente(db, cliente.id, nome="Pedro Souza", nascimento=date(2018, 5, 3))
    criar_plano(db, pedro, fono, [(datetime(2026, 3, 12, 9, 0), AGENDADA)], valor_total="1620.00")
    criar_plano(db, pedro, fono, [(datetime(2026, 3, 20, 9, 0), PENDENTE)], criado_em=CRIADO_VENCIDO, valor_total="999.00")

    html = _html(admin_logado.get(f"/admin/clientes/{cliente.id}"))

    assert "***.***.***-25" in html and "529.982.247-25" not in html
    assert "Pedro Souza" in html and "7 anos" in html and "03/05/2018" not in html
    assert "Carlos Fono" in html and "R$ 1.620,00" in html and ">Ativo</span>" in html
    assert "R$ 999,00" not in html  # pagamento vencido (RN09)
    for texto in ["avaliação", "laudo", "remarcar", "excluir", "desativar"]:
        assert texto not in html.lower()


def test_tela_de_detalhe_sem_cpf(admin_logado, db):
    cliente = criar_cliente(db)
    assert "não informado" in _html(admin_logado.get(f"/admin/clientes/{cliente.id}"))


def test_detalhe_404_para_inexistente_e_para_nao_cliente(admin_logado, db, fono):
    assert admin_logado.get("/admin/clientes/9999").status_code == 404
    assert admin_logado.get(f"/admin/clientes/{fono.usuario.id}").status_code == 404


@pytest.mark.parametrize("perfil", ["cliente", "funcionario"])
@pytest.mark.parametrize("url", ["/admin/clientes", "/admin/clientes/{id}"])
def test_cliente_e_funcionario_nao_acessam(client, db, fono, perfil, url):
    cliente = criar_cliente(db)
    logar(client, cliente.email if perfil == "cliente" else fono.usuario.email)
    assert client.get(url.format(id=cliente.id)).status_code == 403


def test_sem_login_redireciona(client, db):
    cliente = criar_cliente(db)
    for url in ["/admin/clientes", f"/admin/clientes/{cliente.id}"]:
        resposta = client.get(url, follow_redirects=False)
        assert resposta.status_code == 303 and resposta.headers["location"] == "/login"
