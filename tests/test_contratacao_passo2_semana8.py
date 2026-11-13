import re
from datetime import datetime
from pathlib import Path

import pytest

from app.core import templating
from app.models.cliente import Cliente
from app.models.sessao import StatusSessao
from app.routers import cliente as cliente_router
from tests.fabricas import criar_cliente, criar_especialidade, criar_funcionario, criar_paciente, criar_plano, logar

AGORA = datetime(2026, 8, 31, 10, 0)  # segunda, horário local; 07/09 (segunda) é feriado nacional
RAIZ = Path(__file__).resolve().parent.parent
CAMPOS_DO_JSON = {
    "quantidade", "valor_sessao", "valor_bruto", "desconto_percentual", "valor_desconto", "valor_total",
    "data_inicio", "data_fim", "feriados_pulados", "conflitos",
}


@pytest.fixture(autouse=True)
def relogio_fixo(monkeypatch):
    monkeypatch.setattr(cliente_router, "agora_local", lambda: AGORA)


@pytest.fixture
def carla(db):
    return criar_funcionario(db, criar_especialidade(db, "Psicopedagogia", "150.00"), email="carla@exemplo.com", nome="Carla Mendes")


@pytest.fixture
def maria(client, db):
    cliente = criar_cliente(db, email="maria@exemplo.com", nome="Maria Souza")
    logar(client, cliente.email)
    return cliente


def _html(resposta) -> str:
    assert resposta.status_code == 200, resposta.status_code
    return " ".join(resposta.text.split())


def _url(carla, **params):
    return f"/contratar/{carla.usuario.id}", params


# --- Campos do formulário: mesmos nomes e valores ---------------------------------------------------------

def test_radios_com_os_mesmos_nomes_e_valores(client, db, carla, maria):
    ana = criar_paciente(db, maria.id, nome="Ana Souza")
    pedro = criar_paciente(db, maria.id, nome="Pedro Souza")
    html = _html(client.get(f"/contratar/{carla.usuario.id}"))
    assert "<select" not in html.split('id="form-contratacao"')[1]
    nomes = re.findall(r'<input type="radio" class="btn-check" name="(\w+)" id="[\w-]+" value="([\w-]+)"', html)
    assert [v for n, v in nomes if n == "paciente_id"] == [str(ana.id), str(pedro.id)]
    assert [v for n, v in nomes if n == "frequencia_semanal"] == ["1", "2", "3"]
    assert [v for n, v in nomes if n == "duracao"] == ["quinzenal", "mensal", "bimestral", "anual"]
    assert 'id="duracao-mensal" value="mensal" autocomplete="off" checked' in html  # o padrão de hoje
    assert len(re.findall(r'name="celula" value="\d-\d+"', html)) == 5 * 8
    assert "tabela-descontos" not in html and "cartao-profissional-resumido" not in html and "O que é um plano?" not in html
    assert "Com Carla Mendes · Psicopedagogia · <span class=\"font-monospace-num\">R$ 150,00</span> por sessão" in html


def test_post_recebe_os_mesmos_campos(client, db, carla, maria):
    db.get(Cliente, maria.id).cpf = "529.982.247-25"
    db.commit()
    pedro = criar_paciente(db, maria.id, nome="Pedro Souza")
    dados = {"paciente_id": str(pedro.id), "celula": ["0-14"], "frequencia_semanal": "1", "duracao": "quinzenal", "observacao": ""}
    html = _html(client.post(f"/contratar/{carla.usuario.id}", data=dados))
    assert "Pedro Souza" in html and 'name="duracao" value="quinzenal"' in html


def test_percentuais_vem_da_constante(client, db, carla, maria, monkeypatch):
    criar_paciente(db, maria.id)
    html = _html(client.get(f"/contratar/{carla.usuario.id}"))
    assert 'for="duracao-quinzenal">Quinzenal<small>2 semanas</small>' in html  # quinzenal sem percentual
    assert '>Mensal <span class="contr2-desconto">−5%</span><small>1 mês</small>' in html
    monkeypatch.setitem(templating.templates.env.globals, "descontos_por_duracao",
                        {"quinzenal": 0, "mensal": 6, "bimestral": 8, "anual": 12})
    html = _html(client.get(f"/contratar/{carla.usuario.id}"))
    for nome, percentual in (("Mensal", 6), ("Bimestral", 8), ("Anual", 12)):
        assert f'>{nome} <span class="contr2-desconto">−{percentual}%</span>' in html
    assert "−5%" not in html and "−10%" not in html


def test_um_atendido_aparece_so_com_o_nome(client, db, carla, maria):
    pedro = criar_paciente(db, maria.id, nome="Pedro Souza")
    html = _html(client.get(f"/contratar/{carla.usuario.id}"))
    assert '<span class="contr2-rotulo">Pessoa atendida</span> <strong>Pedro Souza</strong>' in html
    assert f'<input type="hidden" name="paciente_id" value="{pedro.id}">' in html
    assert 'id="paciente-' not in html


def test_tres_atendidos_e_pre_selecao(client, db, carla, maria):
    atendidos = [criar_paciente(db, maria.id, nome=n) for n in ("Ana Souza", "Laura Souza", "Pedro Souza")]
    url = f"/contratar/{carla.usuario.id}"
    assert "autocomplete=\"off\" checked> <label class=\"contr2-opcao\" for=\"paciente-" not in _html(client.get(url))
    for atendido in atendidos:
        html = _html(client.get(url, params={"atendido": atendido.id}))
        marcados = re.findall(r'id="paciente-(\d+)" value="\d+" autocomplete="off" checked', html)
        assert marcados == [str(atendido.id)]


# --- Grade ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("frequencia, celulas, contador, motivo", [
    ("1", [], "0 de 1 escolhido", "Falta escolher 1 horário"),
    ("2", [], "0 de 2 escolhidos", "Faltam 2 horários"),
    ("2", ["1-14"], "1 de 2 escolhidos", "Falta escolher 1 horário"),
    ("3", [], "0 de 3 escolhidos", "Faltam 3 horários"),
    ("1", ["0-14"], "1 de 1 escolhido", ""),
])
def test_contador_e_motivo_do_continuar(client, db, carla, maria, frequencia, celulas, contador, motivo):
    criar_paciente(db, maria.id)
    html = _html(client.get(f"/contratar/{carla.usuario.id}", params={"frequencia_semanal": frequencia, "celula": celulas}))
    assert f'<span class="contr2-contador" id="grade-contador">{contador}</span>' in html
    assert f'<p class="contr2-motivo" id="motivo-continuar" aria-live="polite">{motivo}</p>' in html
    assert 'aria-describedby="motivo-continuar"' in html


def test_faixa_do_intervalo(client, db, carla, maria):
    criar_paciente(db, maria.id)
    html = _html(client.get(f"/contratar/{carla.usuario.id}"))
    assert '<th scope="row">12h–14h</th><td colspan="5">12h às 14h, sem atendimento</td>' in html
    assert "Intervalo comercial" not in html


def test_horario_indisponivel_tem_texto_para_leitor_de_tela(client, db, carla, maria):
    criar_paciente(db, maria.id)
    outro = criar_paciente(db, criar_cliente(db, email="outro@exemplo.com").id, nome="Outro")
    criar_plano(db, outro, carla, [(datetime(2026, 9, 2, 14, 0), StatusSessao.AGENDADA)])  # quarta 14h ocupada
    html = _html(client.get(f"/contratar/{carla.usuario.id}"))
    assert re.search(r'value="2-14"[^>]*data-livre="0"[^>]*disabled', html)
    assert re.search(r'<label class="celula celula-ocupada" for="celula-2-14">', html)
    assert '<span class="celula-indisponivel">, indisponível</span>' in html


# --- JSON da simulação e JavaScript ---------------------------------------------------------------------

def test_json_da_simulacao_tem_os_mesmos_campos_e_traz_feriado(client, db, carla, maria):
    dados = client.get(f"/contratar/{carla.usuario.id}/simulacao",
                       params={"dias": "0", "horario": "14:00", "frequencia": "1", "duracao": "quinzenal"}).json()
    assert set(dados) == CAMPOS_DO_JSON  # nenhum campo novo, nenhum removido
    assert (dados["data_inicio"], dados["data_fim"]) == ("31/08/2026", "14/09/2026")
    assert dados["feriados_pulados"] == [{"data": "07/09/2026", "nome": "Independência do Brasil"}]


def test_javascript_continua_um_arquivo_com_os_mesmos_fetch():
    scripts = sorted(p.name for p in (RAIZ / "app" / "static" / "js").glob("*.js"))
    assert scripts == ["contratar.js", "remarcar.js"]
    codigo = (RAIZ / "app" / "static" / "js" / "contratar.js").read_text(encoding="utf-8")
    assert codigo.count("fetch(") == 2  # simulação (cálculo) e disponibilidade (troca de duração), como antes
    assert "urlSimulacao" in codigo and "urlDisponibilidade" in codigo
    assert "grade-contador" in codigo and "motivo-continuar" in codigo


def test_sem_atendido_continua_o_aviso_de_hoje(client, db, carla, maria):
    html = _html(client.get(f"/contratar/{carla.usuario.id}"))
    assert "Cadastre um atendido para contratar" in html and 'id="form-contratacao"' not in html


def test_cpf_aparece_dentro_do_formulario_e_e_salvo_no_perfil(client, db, carla, maria):
    pedro = criar_paciente(db, maria.id, nome="Pedro Souza")
    html = client.get(f"/contratar/{carla.usuario.id}").text
    formulario = re.search(r'<form id="form-contratacao".*?</form>', html, re.S).group(0)
    assert 'name="cpf"' in formulario and "Necessário para gerar a cobrança. Fica salvo no seu perfil." in formulario
    dados = {"paciente_id": str(pedro.id), "celula": ["4-14"], "frequencia_semanal": "1", "duracao": "mensal",
             "observacao": "", "cpf": "529.982.247-25"}
    resumo = _html(client.post(f"/contratar/{carla.usuario.id}", data=dados))
    assert "Seu CPF foi salvo no perfil." in resumo and "Confira e confirme" in resumo
    db.expire_all()
    assert db.get(Cliente, maria.id).cpf == "529.982.247-25"
    assert 'value="529.982.247-25"' in client.get("/perfil").text
    assert 'name="cpf"' not in client.get(f"/contratar/{carla.usuario.id}").text  # com CPF, o campo some
