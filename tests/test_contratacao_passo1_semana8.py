import re
from datetime import datetime

import pytest
from sqlalchemy import event

from app.core.templating import MSG_SEM_PROFISSIONAIS
from app.models.funcionario import Funcionario
from app.models.plano import Plano
from app.routers import cliente as cliente_router
from app.services import funcionario_service
from tests.fabricas import criar_cliente, criar_especialidade, criar_funcionario, criar_paciente, logar

AGORA = datetime(2026, 11, 10, 10, 0)  # horário local


@pytest.fixture(autouse=True)
def relogio_fixo(monkeypatch):
    monkeypatch.setattr(cliente_router, "agora_local", lambda: AGORA)


@pytest.fixture
def pedag(db):
    return criar_especialidade(db, "Psicopedagogia", "150.00")


@pytest.fixture
def carla(db, pedag):
    return criar_funcionario(db, pedag, email="carla.login@exemplo.com", nome="Carla Mendes")


@pytest.fixture
def maria(client, db):
    cliente = criar_cliente(db, email="maria@exemplo.com", nome="Maria Souza")
    logar(client, cliente.email)
    return cliente


def _html(resposta) -> str:
    assert resposta.status_code == 200, resposta.status_code
    return " ".join(resposta.text.split())


def _cartao_esp(html: str, nome: str) -> str:
    cartoes = re.findall(r'<li class="cartao contr-esp.*?</li>', html)
    escolhidos = [c for c in cartoes if f">{nome}</span>" in c]
    assert len(escolhidos) == 1, nome
    return escolhidos[0]


def _cartao_prof(html: str, nome: str) -> str:
    cartoes = re.findall(r'<article class="cartao cartao-profissional".*?</article>', html)
    escolhidos = [c for c in cartoes if f">{nome}</h2>" in c]
    assert len(escolhidos) == 1, nome
    return escolhidos[0]


def _perfil_completo(db, funcionario):
    f = db.get(Funcionario, funcionario.usuario.id)
    f.formacao = "Pedagoga, especialista em Psicopedagogia Clínica"
    f.registro_profissional = "ABPp 1234/RS"
    f.apresentacao = "Atendo crianças com dificuldades de leitura <b>e escrita</b>."
    f.telefone_contato = "(54) 98888-7777"
    f.email_contato = "carla.contato@clinica.exemplo"
    db.commit()


# --- Especialidades ----------------------------------------------------------------------------------------

def test_contagem_de_profissionais_singular_plural_e_so_ativos(client, db, pedag, carla, maria):
    fono = criar_especialidade(db, "Fonoaudiologia", "130.00")
    criar_funcionario(db, fono, email="rafa@exemplo.com", nome="Rafael Lima")
    criar_funcionario(db, fono, email="bia@exemplo.com", nome="Beatriz Rocha")
    desativado = criar_funcionario(db, pedag, email="lia@exemplo.com", nome="Lia Torres")
    funcionario_service.desativar(db, desativado.usuario.id, AGORA)
    html = _html(client.get("/especialidades"))
    assert '<p class="contr-esp-qtd">1 profissional</p>' in _cartao_esp(html, "Psicopedagogia")
    assert '<p class="contr-esp-qtd">2 profissionais</p>' in _cartao_esp(html, "Fonoaudiologia")
    assert '<span class="font-monospace-num">R$ 130,00</span> <small>por sessão</small>' in _cartao_esp(html, "Fonoaudiologia")
    admin = {e.nome: e.ativos for e in funcionario_service.listar_especialidades_com_ativos(db)}
    profissionais_admin = {g.nome: g.ativos for g in funcionario_service.listar_por_especialidade(db, AGORA)}
    assert admin == profissionais_admin == {"Fonoaudiologia": 2, "Psicopedagogia": 1}


def test_especialidade_sem_profissional_apagada_sem_botao(client, db, pedag, carla, maria):
    terapia = criar_especialidade(db, "Terapia Ocupacional", "140.00")
    so_desativado = criar_especialidade(db, "Psicologia", "200.00")
    paula = criar_funcionario(db, so_desativado, email="paula@exemplo.com", nome="Paula Ramos")
    funcionario_service.desativar(db, paula.usuario.id, AGORA)
    html = _html(client.get("/especialidades"))
    for nome, especialidade in (("Terapia Ocupacional", terapia), ("Psicologia", so_desativado)):
        cartao = _cartao_esp(html, nome)
        assert "contr-esp-sem" in cartao
        assert f'<p class="contr-esp-qtd contr-esp-aviso">{MSG_SEM_PROFISSIONAIS}</p>' in cartao
        assert "<a " not in cartao and "profissional<" not in cartao
        assert f"/especialidades/{especialidade.id}/profissionais" not in html
    assert '<li class="cartao contr-esp esp-neutra contr-esp-sem">' in html  # outra especialidade: tom neutro
    assert "contr-esp-sem" not in _cartao_esp(html, "Psicopedagogia")


def test_titulo_explicacao_curta_e_etapas(client, db, carla, maria):
    html = _html(client.get("/especialidades"))
    assert "Escolha a especialidade</h1>" in html
    assert "Veja o valor de cada sessão e escolha uma área para conhecer os profissionais." in html
    assert '<p class="contr-plano">' in html and "as sessões são criadas automaticamente nos dias e horários escolhidos" in html
    assert 'aria-label="Etapas da contratação"' in html and 'aria-current="step"' in html


def test_url_direta_de_especialidade_sem_profissional(client, db, maria):
    terapia = criar_especialidade(db, "Terapia Ocupacional", "140.00")
    html = _html(client.get(f"/especialidades/{terapia.id}/profissionais"))
    assert "Nenhum profissional disponível no momento" in html


# --- Profissionais ---------------------------------------------------------------------------------------

def test_botao_com_primeiro_nome_e_mesmo_destino(client, db, pedag, carla, maria):
    html = _html(client.get(f"/especialidades/{pedag.id}/profissionais"))
    cartao = _cartao_prof(html, "Carla Mendes")
    assert f'<a href="/contratar/{carla.usuario.id}" class="btn btn-primary btn-sm align-self-start mt-1"> Contratar com Carla' in cartao
    assert "Escolha com quem fazer o plano. Valor da sessão:" in html


def test_campos_opcionais_so_quando_preenchidos(client, db, pedag, carla, maria):
    beatriz = criar_funcionario(db, pedag, email="bia.login@exemplo.com", nome="Beatriz Rocha")
    _perfil_completo(db, carla)
    html = _html(client.get(f"/especialidades/{pedag.id}/profissionais"))
    cheio = _cartao_prof(html, "Carla Mendes")
    for classe in ("perfil-registro", "perfil-formacao", "perfil-apresentacao", "perfil-contato"):
        assert classe in cheio, classe
    vazio = _cartao_prof(html, "Beatriz Rocha")
    for classe in ("perfil-registro", "perfil-formacao", "perfil-apresentacao", "perfil-contato"):
        assert classe not in vazio, classe
    assert f'href="/contratar/{beatriz.usuario.id}"' in vazio and "Contratar com Beatriz" in vazio


def test_email_de_login_nunca_aparece(client, db, pedag, carla, maria):
    _perfil_completo(db, carla)
    sem_contato = criar_funcionario(db, pedag, email="bia.login@exemplo.com", nome="Beatriz Rocha")
    paginas = [client.get(f"/especialidades/{pedag.id}/profissionais").text, client.get("/especialidades").text]
    for html in paginas:
        assert "carla.login@exemplo.com" not in html and "bia.login@exemplo.com" not in html
    assert "carla.contato@clinica.exemplo" in paginas[0]  # o e-mail de contato aparece
    assert sem_contato.usuario.email == "bia.login@exemplo.com"


def test_apresentacao_escapada(client, db, pedag, carla, maria):
    _perfil_completo(db, carla)
    html = client.get(f"/especialidades/{pedag.id}/profissionais").text
    assert "<b>e escrita</b>" not in html and "&lt;b&gt;e escrita&lt;/b&gt;" in html


# --- Sem atendido ------------------------------------------------------------------------------------------

def test_bloco_ambar_sem_atendido_nas_duas_telas(client, db, pedag, carla, maria):
    for url in ("/especialidades", f"/especialidades/{pedag.id}/profissionais"):
        html = _html(client.get(url))
        bloco = re.search(r'<div class="contr-sem-atendido" role="note">.*?</div>', html).group(0)
        assert "Antes de contratar, cadastre a pessoa que será atendida." in bloco
        assert '<a href="/pacientes/novo" class="btn btn-primary btn-sm">Cadastrar atendido</a>' in bloco
    profissionais = _html(client.get(f"/especialidades/{pedag.id}/profissionais"))
    assert f'href="/contratar/{carla.usuario.id}"' in profissionais  # o botão continua como antes (decisão de 10/11)


def test_bloco_ambar_some_com_atendido(client, db, pedag, carla, maria):
    criar_paciente(db, maria.id)
    for url in ("/especialidades", f"/especialidades/{pedag.id}/profissionais"):
        assert "contr-sem-atendido" not in _html(client.get(url))


# --- Pré-seleção do atendido -------------------------------------------------------------------------------

def _selecionado(html: str) -> list[str]:
    return re.findall(r'name="paciente_id" id="paciente-\d+" value="(\d+)" autocomplete="off" checked', html)


def test_parametro_repassado_no_passo_1(client, db, pedag, carla, maria):
    ana = criar_paciente(db, maria.id, nome="Ana Souza")
    criar_especialidade(db, "Terapia Ocupacional", "140.00")
    html = _html(client.get(f"/especialidades?atendido={ana.id}"))
    assert f'href="/especialidades/{pedag.id}/profissionais?atendido={ana.id}"' in html
    assert "contr-sem-atendido" not in html
    assert "conhecer os profissionais que podem atender Ana Souza." in html
    profissionais = _html(client.get(f"/especialidades/{pedag.id}/profissionais?atendido={ana.id}"))
    assert f'href="/contratar/{carla.usuario.id}?atendido={ana.id}"' in profissionais
    assert f'href="/especialidades?atendido={ana.id}" class="pagina-voltar"' in profissionais


def test_passo_2_vem_com_o_atendido_escolhido(client, db, carla, maria):
    ana = criar_paciente(db, maria.id, nome="Ana Souza")
    pedro = criar_paciente(db, maria.id, nome="Pedro Souza")
    url = f"/contratar/{carla.usuario.id}"
    assert _selecionado(_html(client.get(f"{url}?atendido={ana.id}"))) == [str(ana.id)]
    assert _selecionado(_html(client.get(f"{url}?atendido={pedro.id}"))) == [str(pedro.id)]
    assert _selecionado(_html(client.get(url))) == []  # sem o parâmetro, nada muda


def test_sem_parametro_nada_muda(client, db, pedag, carla, maria):
    criar_paciente(db, maria.id)
    html = _html(client.get("/especialidades"))
    assert "?atendido=" not in html
    assert "?atendido=" not in _html(client.get(f"/especialidades/{pedag.id}/profissionais"))


def test_botao_do_cartao_de_atendidos_leva_o_id_certo(client, db, maria):
    ana = criar_paciente(db, maria.id, nome="Ana Souza")
    pedro = criar_paciente(db, maria.id, nome="Pedro Souza")
    html = _html(client.get("/pacientes"))
    for atendido in (ana, pedro):
        assert f'href="/especialidades?atendido={atendido.id}"' in html


@pytest.mark.parametrize("valor", ["", "abc", "0", "-1", "²", "1.5", "99999999999999999999", "9999", "ALHEIO"])
def test_atendido_invalido_ou_alheio_da_404_nos_passos_1_e_2(client, db, pedag, carla, maria, valor):
    alheio = criar_paciente(db, criar_cliente(db, email="outro@exemplo.com", nome="Outra Pessoa").id, nome="Filho do Outro")
    valor = str(alheio.id) if valor == "ALHEIO" else valor
    for url in ("/especialidades", f"/especialidades/{pedag.id}/profissionais", f"/contratar/{carla.usuario.id}"):
        resposta = client.get(url, params={"atendido": valor})
        assert resposta.status_code == 404, (url, valor)
        assert "Filho do Outro" not in resposta.text


def test_atendido_alheio_responde_igual_a_inexistente(client, db, carla, maria):
    alheio = criar_paciente(db, criar_cliente(db, email="outro@exemplo.com").id, nome="Filho do Outro")
    url = f"/contratar/{carla.usuario.id}"
    alheia = client.get(url, params={"atendido": str(alheio.id)})
    inexistente = client.get(url, params={"atendido": "999999"})
    assert (alheia.status_code, alheia.text) == (inexistente.status_code, inexistente.text) == (404, inexistente.text)


def test_post_da_confirmacao_nao_depende_do_parametro(client, db, carla, maria):
    meu = criar_paciente(db, maria.id, nome="Ana Souza")
    alheio = criar_paciente(db, criar_cliente(db, email="outro@exemplo.com").id, nome="Filho do Outro")
    dados = {"paciente_id": str(alheio.id), "celula": ["0-14"], "frequencia_semanal": "1", "duracao": "mensal", "observacao": ""}
    for url in (f"/contratar/{carla.usuario.id}?atendido={meu.id}", f"/contratar/{carla.usuario.id}/confirmar?atendido={meu.id}"):
        assert client.post(url, data=dados).status_code == 404
    assert db.query(Plano).count() == 0


# --- SQL -----------------------------------------------------------------------------------------------------

def _contar_sql(client, db, url) -> int:
    comandos = []
    ouvinte = lambda *args: comandos.append(args[2])  # noqa: E731
    event.listen(db.get_bind(), "before_cursor_execute", ouvinte)
    try:
        assert client.get(url).status_code == 200
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", ouvinte)
    return len(comandos)


def test_especialidades_com_poucas_e_muitas_fazem_o_mesmo_numero_de_consultas(client, db, pedag, carla, maria):
    criar_paciente(db, maria.id)
    _contar_sql(client, db, "/especialidades")  # aquece: a 1ª requisição recarrega o usuário logado
    poucas = _contar_sql(client, db, "/especialidades")
    for i in range(12):
        especialidade = criar_especialidade(db, f"Especialidade {i:02d}", "100.00")
        for j in range(i % 3):
            criar_funcionario(db, especialidade, email=f"p{i}{j}@exemplo.com", nome=f"P{i}{j} Nome")
    db.expire_all()
    _contar_sql(client, db, "/especialidades")
    assert _contar_sql(client, db, "/especialidades") == poucas == 2  # especialidades com ativos + atendidos
