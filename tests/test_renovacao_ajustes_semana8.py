import re
from datetime import datetime, timezone

import pytest

from app.models.cliente import Cliente
from app.models.sessao import StatusSessao
from app.routers import cliente as cliente_router
from app.services import contratacao_service
from tests.fabricas import criar_cliente, criar_especialidade, criar_funcionario, criar_paciente, criar_plano, logar

AGENDADA = StatusSessao.AGENDADA
REALIZADA = StatusSessao.REALIZADA
AGORA = datetime(2026, 8, 1, 10, 0)  # sábado, horário local
JULHO = [(datetime(2026, 7, dia, 14, 0), REALIZADA) for dia in (6, 13, 20, 27)]  # segundas, 14:00


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


def _quem(html: str) -> str:
    return re.search(r'<section class="cartao mb-4 contr2-bloco" aria-labelledby="titulo-quem">.*?</section>', html).group(0)


def _ocupar_segunda(db, carla):
    alheio = criar_paciente(db, criar_cliente(db, email="outra@exemplo.com").id, nome="Atendido Alheio")
    criar_plano(db, alheio, carla, [(datetime(2026, 8, 17, 14, 0), AGENDADA)], criado_em=datetime(2026, 7, 30, 12, 0))


# --- 1. Passo 2 da renovação: atendido fixo -------------------------------------------------------------

def test_editar_mostra_so_o_atendido_do_plano_anterior(client, db, maria, pedro, encerrado):
    criar_paciente(db, maria.id, nome="Ana Souza")
    criar_paciente(db, maria.id, nome="Bia Souza")
    quem = _quem(_html(client.get(f"/planos/{encerrado.id}/renovar?editar=1")))
    assert '<p class="contr2-unico"><span class="contr2-rotulo">Pessoa atendida</span> <strong>Pedro Souza</strong></p>' in quem
    assert f'<input type="hidden" name="paciente_id" value="{pedro.id}">' in quem
    assert 'type="radio"' not in quem.split("Quantas vezes por semana")[0]
    assert "Ana Souza" not in quem and "Bia Souza" not in quem


def test_contratacao_normal_continua_com_os_radios(client, db, maria, pedro, carla):
    criar_paciente(db, maria.id, nome="Ana Souza")
    quem = _quem(_html(client.get(f"/contratar/{carla.usuario.id}")))
    assert quem.count('name="paciente_id"') == 2 and "Ana Souza" in quem


def test_post_do_passo_2_continua_conferindo_o_dono(client, db, carla, encerrado):
    alheio = criar_paciente(db, criar_cliente(db, email="outra@exemplo.com").id, nome="Atendido Alheio")
    dados = {"paciente_id": str(alheio.id), "celula": ["0-14"], "frequencia_semanal": "1", "duracao": "mensal", "observacao": ""}
    assert client.post(f"/contratar/{carla.usuario.id}", data=dados).status_code == 404


# --- 2. Mensagem de horário ocupado ----------------------------------------------------------------------

def test_horario_ocupado_sem_outro_profissional_nao_cita_a_troca(client, db, carla, encerrado):
    _ocupar_segunda(db, carla)
    alerta = re.search(r'<div class="rev-conflito" role="alert" id="aviso-renovacao">.*?</div>',
                       _html(client.get(f"/planos/{encerrado.id}/renovar"))).group(0)
    assert ("O horário do plano anterior (Segunda, às 14:00) não está mais livre com Carla Fono em alguma data do novo "
            "período. Escolha outro dia ou horário.") in alerta
    assert "outro profissional" not in alerta and "em todo o novo período" not in alerta


def test_horario_ocupado_com_outro_profissional_cita_a_troca(client, db, fono, carla, encerrado):
    _ocupar_segunda(db, carla)
    criar_funcionario(db, fono, email="rafael@exemplo.com", nome="Rafael Lima")
    html = _html(client.get(f"/planos/{encerrado.id}/renovar"))
    alerta = re.search(r'<div class="rev-conflito" role="alert" id="aviso-renovacao">.*?</div>', html).group(0)
    assert "em alguma data do novo período. Escolha outro dia ou horário, ou outro profissional." in alerta
    assert 'id="renovacao-profissionais"' in html and ">Rafael Lima</a>" in html


@pytest.mark.parametrize("outros, final", [
    ([], "Escolha outro dia ou horário."),
    (["Rafael"], "Escolha outro dia ou horário, ou outro profissional."),
])
def test_final_da_mensagem_depende_do_link(outros, final):
    alternativa = contratacao_service.ALTERNATIVA_OUTRO_PROFISSIONAL if outros else ""
    texto = contratacao_service.MSG_HORARIO_OCUPADO.format(dias="Segunda", hora="14:00", nome="Carla", alternativa=alternativa)
    assert texto.endswith(final)


# --- 3. Feriados pulados no singular e no plural ---------------------------------------------------------

@pytest.mark.parametrize("duracao", ["bimestral", "anual"])
def test_feriados_pulados_com_plural_correto(client, db, encerrado, duracao):
    encerrado.duracao = duracao
    db.commit()
    html = _html(client.get(f"/planos/{encerrado.id}/renovar"))
    assert "feriado(s)" not in html and "pulado(s)" not in html
    quantidade = int(re.search(r"(\d+) feriados? pulados?;", html).group(1))
    if duracao == "bimestral":  # 07/09 é segunda
        assert quantidade == 1 and "1 feriado pulado; o plano se estende sem mudar o valor." in html
    else:  # 07/09, 12/10 e 02/11 são segundas
        assert quantidade >= 2 and f"{quantidade} feriados pulados; o plano se estende sem mudar o valor." in html
