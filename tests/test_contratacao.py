import re
from datetime import datetime

import pytest

from app.models.cliente import Cliente
from app.models.pagamento import Pagamento
from app.models.plano import Plano
from app.models.sessao import StatusSessao
from app.routers import cliente as cliente_router
from app.services import funcionario_service
from tests.fabricas import (
    criar_admin,
    criar_cliente,
    criar_especialidade,
    criar_funcionario,
    criar_paciente,
    criar_plano_com_sessao,
    logar,
)

AGORA = datetime(2026, 8, 1, 10, 0)  # sábado, horário local
CPF_VALIDO = "529.982.247-25"
OUTRO_CPF_VALIDO = "111.444.777-35"


@pytest.fixture(autouse=True)
def relogio_fixo(monkeypatch):
    monkeypatch.setattr(cliente_router, "agora_local", lambda: AGORA)


@pytest.fixture
def fono(db):
    return criar_especialidade(db, "Fonoaudiologia", "150.00")


@pytest.fixture
def carla(db, fono):
    return criar_funcionario(db, fono, email="carla@exemplo.com", nome="Carla Fono")


@pytest.fixture
def maria(db):
    return criar_cliente(db, email="maria@exemplo.com", nome="Maria Souza")


@pytest.fixture
def pedro(db, maria):
    return criar_paciente(db, maria.id, nome="Pedro Souza")


@pytest.fixture
def maria_logada(client, maria):
    logar(client, maria.email)
    return maria


def _url(funcionario):
    return f"/contratar/{funcionario.usuario.id}"


def _form(paciente, celulas=("0-14",), frequencia="1", duracao="mensal", **extras):
    return {"paciente_id": str(paciente.id), "celula": list(celulas), "frequencia_semanal": frequencia,
            "duracao": duracao, "observacao": "", **extras}


def _ocupar_por_outro_cliente(db, funcionario, especialidade, data_hora, status=StatusSessao.AGENDADA):
    return criar_plano_com_sessao(
        db, funcionario.usuario.id, especialidade, status, data_hora=data_hora,
        criado_em=datetime(2026, 8, 1, 12, 0), email_cliente="joana@exemplo.com",
    )


def _nada_foi_criado(db, planos_antes=0):
    assert db.query(Plano).count() == planos_antes
    assert db.query(Pagamento).count() == 0


# --- Acesso --------------------------------------------------------------------------------------

@pytest.mark.parametrize("perfil", ["admin", "funcionario"])
def test_outros_perfis_recebem_403(client, db, fono, carla, perfil):
    email = criar_admin(db).email if perfil == "admin" else criar_funcionario(db, fono, email="f2@exemplo.com").usuario.email
    logar(client, email)
    url = _url(carla)
    assert client.get(url).status_code == 403
    assert client.get(f"{url}/disponibilidade?duracao=mensal").status_code == 403
    assert client.get(f"{url}/simulacao?dias=0&horario=14:00&frequencia=1&duracao=mensal").status_code == 403
    assert client.post(url, data={}).status_code == 403


def test_sem_login_redireciona_para_login(client, carla):
    url = _url(carla)
    for resposta in (
        client.get(url, follow_redirects=False),
        client.get(f"{url}/disponibilidade", follow_redirects=False),
        client.get(f"{url}/simulacao", follow_redirects=False),
        client.post(url, data={}, follow_redirects=False),
    ):
        assert resposta.status_code == 303 and resposta.headers["location"] == "/login"


@pytest.mark.parametrize("situacao", ["inexistente", "inativo"])
def test_profissional_inexistente_ou_inativo_retorna_404(client, db, carla, pedro, maria_logada, situacao):
    if situacao == "inativo":
        funcionario_service.desativar(db, carla.usuario.id, AGORA)
        url = _url(carla)
    else:
        url = "/contratar/999"
    assert client.get(url).status_code == 404
    assert client.get(f"{url}/disponibilidade").status_code == 404
    assert client.get(f"{url}/simulacao?dias=0&horario=14:00&frequencia=1&duracao=mensal").status_code == 404
    assert client.post(url, data=_form(pedro, cpf=CPF_VALIDO)).status_code == 404


# --- Tela de contratação ---------------------------------------------------------------------------

def test_sem_pacientes_mostra_aviso_no_lugar_do_formulario(client, carla, maria_logada):
    html = client.get(_url(carla)).text
    assert "Cadastre um paciente para contratar" in html
    assert 'href="/pacientes/novo"' in html
    assert 'id="form-contratacao"' not in html


def test_tela_mostra_profissional_grade_e_so_pacientes_do_cliente(client, db, fono, carla, pedro, maria_logada):
    outra = criar_cliente(db, email="outra@exemplo.com", nome="Outra Cliente")
    criar_paciente(db, outra.id, nome="Paciente Alheio")
    _ocupar_por_outro_cliente(db, carla, fono, datetime(2026, 8, 12, 14, 0))

    html = client.get(_url(carla)).text
    assert "Carla Fono" in html and "Fonoaudiologia" in html and "R$ 150,00" in html
    assert "Pedro Souza" in html and "Paciente Alheio" not in html
    assert re.search(r'value="2-14"[^>]*data-livre="0"[^>]*disabled', html)
    assert re.search(r'value="0-14"[^>]*data-livre="1"', html)
    assert 'value="0-12"' not in html and 'value="0-13"' not in html
    assert "Intervalo de almoço" in html
    assert "joana@exemplo.com" not in html


def test_cliente_sem_cpf_ve_o_campo_e_com_cpf_nao_ve(client, db, carla, pedro, maria, maria_logada):
    assert 'name="cpf"' in client.get(_url(carla)).text
    db.get(Cliente, maria.id).cpf = CPF_VALIDO
    db.commit()
    assert 'name="cpf"' not in client.get(_url(carla)).text


def test_botao_contratar_na_lista_de_profissionais(client, fono, carla, maria_logada):
    html = client.get(f"/especialidades/{fono.id}/profissionais").text
    assert f'href="/contratar/{carla.usuario.id}"' in html and "Contratar" in html


# --- Endpoints JSON --------------------------------------------------------------------------------

def test_grade_json_so_revela_livre_ou_ocupado(client, db, fono, carla, maria_logada):
    _ocupar_por_outro_cliente(db, carla, fono, datetime(2026, 8, 12, 14, 0))

    resposta = client.get(f"{_url(carla)}/disponibilidade?duracao=mensal")
    dados = resposta.json()
    assert resposta.status_code == 200
    assert set(dados) == {"duracao", "livres"}
    assert set(dados["livres"]) == {"0", "1", "2", "3", "4"}
    assert all(isinstance(hora, int) for horas in dados["livres"].values() for hora in horas)
    assert 14 not in dados["livres"]["2"] and 14 in dados["livres"]["0"]
    for vazamento in ("Pedro", "Souza", "joana", "pendente", "agendada", "2026-08-12", "12/08"):
        assert vazamento not in resposta.text


def test_grade_json_com_duracao_invalida(client, carla, maria_logada):
    resposta = client.get(f"{_url(carla)}/disponibilidade?duracao=semestral")
    assert resposta.status_code == 400 and "erro" in resposta.json()


def test_simulacao_json(client, carla, maria_logada):
    resposta = client.get(f"{_url(carla)}/simulacao?dias=0&horario=14:00&frequencia=1&duracao=bimestral")
    assert resposta.status_code == 200
    assert resposta.json() == {
        "quantidade": 9,
        "valor_sessao": "R$ 150,00",
        "valor_total": "R$ 1.350,00",
        "data_inicio": "03/08/2026",
        "data_fim": "05/10/2026",
        "feriados_pulados": [{"data": "07/09/2026", "nome": "Independência do Brasil"}],
        "conflitos": [],
    }


def test_simulacao_json_mostra_conflitos_sem_dados_de_outros(client, db, fono, carla, maria_logada):
    _ocupar_por_outro_cliente(db, carla, fono, datetime(2026, 8, 10, 14, 0))
    resposta = client.get(f"{_url(carla)}/simulacao?dias=0&horario=14:00&frequencia=1&duracao=mensal")
    assert resposta.json()["conflitos"] == ["10/08/2026 às 14:00"]
    assert "joana" not in resposta.text and "Pedro" not in resposta.text


@pytest.mark.parametrize(
    "consulta, trecho",
    [
        ("dias=0&horario=12:00&frequencia=1&duracao=mensal", "hora cheia"),
        ("dias=0&horario=14:30&frequencia=1&duracao=mensal", "hora cheia"),
        ("dias=5&horario=14:00&frequencia=1&duracao=mensal", "segunda a sexta"),
        ("dias=0&horario=14:00&frequencia=4&duracao=mensal", "1, 2 ou 3"),
        ("dias=0&horario=14:00&frequencia=x&duracao=mensal", "1, 2 ou 3"),
        ("dias=0&horario=14:00&frequencia=1&duracao=semestral", "quinzenal, mensal"),
        ("dias=0&horario=abc&frequencia=1&duracao=mensal", "Horário inválido"),
    ],
)
def test_simulacao_json_com_entrada_invalida(client, carla, maria_logada, consulta, trecho):
    resposta = client.get(f"{_url(carla)}/simulacao?{consulta}")
    assert resposta.status_code == 400 and trecho in resposta.json()["erro"]


# --- POST: resumo -----------------------------------------------------------------------------------

def test_resumo_mostra_o_plano_sem_criar_nada(client, db, fono, carla, pedro, maria, maria_logada):
    db.get(Cliente, maria.id).cpf = CPF_VALIDO
    db.commit()
    resposta = client.post(_url(carla), data=_form(pedro, celulas=("0-14",), duracao="bimestral", observacao="Prefere tarde"))

    html = resposta.text
    assert resposta.status_code == 200
    for trecho in (
        "Fonoaudiologia", "Carla Fono", "Pedro Souza", "Segunda, às 14:00", "1x por semana", "Bimestral (2 meses)",
        "9 sessões", "03/08/2026 a 05/10/2026", "07/09/2026 — Independência do Brasil",
        "R$ 150,00", "R$ 1.350,00", "4 horas", "Voltar e editar", "Prefere tarde",
    ):
        assert trecho in html, trecho
    assert "05/10/2026" in html and "07/09/2026</span>" not in html
    assert "Confirmar" not in html
    _nada_foi_criado(db)


def test_resumo_com_duas_sessoes_por_semana(client, db, carla, pedro, maria, maria_logada):
    db.get(Cliente, maria.id).cpf = CPF_VALIDO
    db.commit()
    html = client.post(_url(carla), data=_form(pedro, celulas=("2-9", "0-9"), frequencia="2", duracao="quinzenal")).text
    assert "Segunda e Quarta, às 09:00" in html and "4 sessões" in html and "R$ 600,00" in html


def test_voltar_e_editar_preserva_a_selecao(client, db, carla, pedro, maria, maria_logada):
    db.get(Cliente, maria.id).cpf = CPF_VALIDO
    db.commit()
    html = client.post(_url(carla), data=_form(pedro, celulas=("4-16",), duracao="quinzenal")).text
    url_editar = re.search(r'href="(/contratar/[^"]+)"[^>]*>\s*<i class="bi bi-arrow-left', html).group(1).replace("&amp;", "&")
    tela = client.get(url_editar).text
    assert re.search(r'value="4-16"[^>]*checked', tela)
    assert re.search(r'<option value="quinzenal" selected', tela)
    assert re.search(rf'<option value="{pedro.id}" selected', tela)


def test_revalidacao_ignora_numeros_adulterados(client, db, carla, pedro, maria, maria_logada):
    db.get(Cliente, maria.id).cpf = CPF_VALIDO
    db.commit()
    dados = _form(pedro, valor_total="1.00", quantidade="1", valor_sessao="0.01", data_fim="2026-08-03")
    html = client.post(_url(carla), data=dados).text
    assert "R$ 750,00" in html and "5 sessões" in html
    assert "R$ 1,00" not in html and "R$ 0,01" not in html


@pytest.mark.parametrize(
    "celulas, frequencia, trecho",
    [
        (("0-12",), "1", "hora cheia"),
        (("0-14", "2-15"), "2", "mesmo horário"),
        (("5-14",), "1", "segunda a sexta"),
        (("0-14", "0-14"), "2", "dias da semana diferentes"),
        (("0-14",), "2", "exatamente 2"),
        ((), "1", "Escolha os horários"),
        (("x-14",), "1", "Seleção de horário inválida"),
    ],
)
def test_celulas_adulteradas_sao_rejeitadas(client, db, carla, pedro, maria, maria_logada, celulas, frequencia, trecho):
    db.get(Cliente, maria.id).cpf = CPF_VALIDO
    db.commit()
    resposta = client.post(_url(carla), data=_form(pedro, celulas=celulas, frequencia=frequencia))
    assert resposta.status_code == 400
    assert trecho in resposta.text
    assert 'id="form-contratacao"' in resposta.text and "Voltar e editar" not in resposta.text


@pytest.mark.parametrize("campo, valor, trecho", [
    ("frequencia_semanal", "4", "1, 2 ou 3"),
    ("duracao", "semestral", "quinzenal, mensal, bimestral ou anual"),
    ("paciente_id", "", "Selecione o paciente"),
    ("observacao", "x" * 501, "no máximo 500"),
])
def test_campos_invalidos_voltam_ao_formulario(client, db, carla, pedro, maria, maria_logada, campo, valor, trecho):
    db.get(Cliente, maria.id).cpf = CPF_VALIDO
    db.commit()
    resposta = client.post(_url(carla), data={**_form(pedro), campo: valor})
    assert resposta.status_code == 400 and trecho in resposta.text and 'id="form-contratacao"' in resposta.text


def test_paciente_de_outro_cliente_retorna_404(client, db, carla, maria, maria_logada):
    outra = criar_cliente(db, email="outra@exemplo.com")
    alheio = criar_paciente(db, outra.id, nome="Paciente Alheio")
    resposta = client.post(_url(carla), data=_form(alheio, cpf=CPF_VALIDO))
    assert resposta.status_code == 404
    assert db.get(Cliente, maria.id).cpf is None


def test_conflito_bloqueia_o_resumo_e_mostra_as_datas(client, db, fono, carla, pedro, maria, maria_logada):
    _ocupar_por_outro_cliente(db, carla, fono, datetime(2026, 8, 17, 14, 30), StatusSessao.PENDENTE_PAGAMENTO)
    resposta = client.post(_url(carla), data=_form(pedro, cpf=CPF_VALIDO))

    assert resposta.status_code == 409
    assert "17/08/2026 às 14:00" in resposta.text
    assert "já estão ocupados" in resposta.text
    assert 'id="form-contratacao"' in resposta.text and "Voltar e editar" not in resposta.text
    assert "joana" not in resposta.text
    assert db.get(Cliente, maria.id).cpf is None
    _nada_foi_criado(db, planos_antes=1)


# --- CPF na tela de contratação ---------------------------------------------------------------------

@pytest.mark.parametrize("cpf, mensagem", [
    ("", "Informe o seu CPF"),
    ("123.456.789-00", "Informe um CPF válido."),
    ("111.111.111-11", "Informe um CPF válido."),
])
def test_cpf_invalido_ou_vazio(client, db, carla, pedro, maria, maria_logada, cpf, mensagem):
    resposta = client.post(_url(carla), data=_form(pedro, cpf=cpf))
    assert resposta.status_code == 400 and mensagem in resposta.text
    assert re.search(r'id="cpf"[^>]*class="form-control is-invalid"', resposta.text)
    assert db.get(Cliente, maria.id).cpf is None


def test_cpf_ja_usado_por_outro_cliente(client, db, carla, pedro, maria, maria_logada):
    outra = criar_cliente(db, email="outra@exemplo.com")
    db.get(Cliente, outra.id).cpf = CPF_VALIDO
    db.commit()
    resposta = client.post(_url(carla), data=_form(pedro, cpf="52998224725"))
    assert resposta.status_code == 400 and "Este CPF já está cadastrado em outra conta." in resposta.text
    assert db.get(Cliente, maria.id).cpf is None


def test_cpf_valido_e_gravado_quando_a_simulacao_e_valida(client, db, carla, pedro, maria, maria_logada):
    resposta = client.post(_url(carla), data=_form(pedro, cpf="11144477735"))
    assert resposta.status_code == 200 and "Seu CPF foi salvo no perfil." in resposta.text
    db.expire_all()
    assert db.get(Cliente, maria.id).cpf == OUTRO_CPF_VALIDO
    assert 'name="cpf"' not in client.get(_url(carla)).text


def test_cpf_valido_nao_e_gravado_se_outro_campo_falhar(client, db, carla, pedro, maria, maria_logada):
    resposta = client.post(_url(carla), data=_form(pedro, celulas=("0-12",), cpf=CPF_VALIDO))
    assert resposta.status_code == 400
    assert db.get(Cliente, maria.id).cpf is None


def test_cliente_com_cpf_ignora_cpf_enviado(client, db, carla, pedro, maria, maria_logada):
    db.get(Cliente, maria.id).cpf = CPF_VALIDO
    db.commit()
    resposta = client.post(_url(carla), data=_form(pedro, cpf=OUTRO_CPF_VALIDO))
    assert resposta.status_code == 200
    db.expire_all()
    assert db.get(Cliente, maria.id).cpf == CPF_VALIDO


def test_navbar_marca_profissionais_na_contratacao_e_no_resumo(client, db, carla, pedro, maria, maria_logada):
    ativo = 'class="nav-link active" href="/especialidades" aria-current="page"'
    assert ativo in client.get(_url(carla)).text
    db.get(Cliente, maria.id).cpf = CPF_VALIDO
    db.commit()
    resumo = client.post(_url(carla), data=_form(pedro))
    assert resumo.status_code == 200 and "Voltar e editar" in resumo.text and ativo in resumo.text
    assert resumo.text.count('class="nav-link active"') == 1
