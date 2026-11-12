import re
from decimal import Decimal

import pytest
from sqlalchemy import event

from app.core import templating
from app.core.config import DESCONTO_POR_DURACAO
from app.core.templating import MSG_SEM_PROFISSIONAL_ATIVO
from app.models.especialidade import Especialidade
from app.models.plano import Plano
from app.models.sessao import StatusSessao
from app.routers.admin import MSG_ESPECIALIDADE_REPETIDA
from app.services import funcionario_service
from tests.fabricas import (
    AGORA,
    criar_admin,
    criar_cliente,
    criar_especialidade,
    criar_funcionario,
    criar_plano_com_sessao,
    logar,
)

URL = "/admin/especialidades"


@pytest.fixture
def admin(client, db):
    logar(client, criar_admin(db).email)
    return client


def _prof(db, especialidade, nome, desativado=False):
    resumo = criar_funcionario(db, especialidade, email=f"{nome.split()[0].lower()}@exemplo.com", nome=nome)
    if desativado:
        funcionario_service.desativar(db, resumo.usuario.id, AGORA)
    return resumo


def _linha(html: str, nome: str) -> str:
    linhas = re.findall(r"<tr>.*?</tr>", html, re.S)
    escolhidas = [linha for linha in linhas if f">{nome}</span>" in linha]
    assert len(escolhidas) == 1, nome
    return " ".join(escolhidas[0].split())


# --- Coluna Profissionais ------------------------------------------------------------------------

def test_coluna_profissionais_singular_plural_e_sem_ativo(db, admin):
    fono = criar_especialidade(db, "Fonoaudiologia", "150.00")
    pedag = criar_especialidade(db, "Psicopedagogia", "180.00")
    terapia = criar_especialidade(db, "Terapia Ocupacional", "160.00")
    _prof(db, fono, "Carla Lima")
    _prof(db, pedag, "Juliana Prado")
    _prof(db, pedag, "Rui Costa")
    html = admin.get(URL).text

    assert f'href="/admin/funcionarios#grupo-{fono.id}" class="esp-profissionais">1 ativo<' in _linha(html, "Fonoaudiologia")
    assert f'href="/admin/funcionarios#grupo-{pedag.id}" class="esp-profissionais">2 ativos<' in _linha(html, "Psicopedagogia")
    sem = _linha(html, "Terapia Ocupacional")
    assert f'href="/admin/funcionarios#grupo-{terapia.id}" class="esp-sem-ativo">Sem profissional ativo<' in sem
    assert "ativos<" not in sem and "1 ativo" not in sem


def test_desativado_nao_conta_como_ativo(db, admin):
    fono = criar_especialidade(db, "Fonoaudiologia", "150.00")
    psico = criar_especialidade(db, "Psicologia", "200.00")
    _prof(db, fono, "Carla Lima")
    _prof(db, fono, "Rui Costa", desativado=True)
    _prof(db, psico, "Lia Torres", desativado=True)
    html = admin.get(URL).text
    assert ">1 ativo<" in _linha(html, "Fonoaudiologia")
    assert MSG_SEM_PROFISSIONAL_ATIVO in _linha(html, "Psicologia")


def test_contagem_igual_a_da_lista_de_profissionais(db, admin):
    especialidades = [criar_especialidade(db, nome, "100.00") for nome in ("Alfa", "Beta", "Gama", "Delta")]
    for i, especialidade in enumerate(especialidades):
        for j in range(i):
            _prof(db, especialidade, f"P{i}{j} Nome", desativado=(j == 1))
    grupos = {g.id: g.ativos for g in funcionario_service.listar_por_especialidade(db, AGORA)}
    lista = {e.id: e.ativos for e in funcionario_service.listar_especialidades_com_ativos(db)}
    confirmacao = {e.id: funcionario_service.contar_ativos_da_especialidade(db, e.id) for e in especialidades}
    assert grupos == lista == confirmacao == {especialidades[0].id: 0, especialidades[1].id: 1,
                                              especialidades[2].id: 1, especialidades[3].id: 2}


def test_frase_sem_ativo_e_a_mesma_nas_duas_listas(db, admin):
    _prof(db, criar_especialidade(db, "Terapia Ocupacional", "160.00"), "Lia Torres", desativado=True)
    assert MSG_SEM_PROFISSIONAL_ATIVO == "Sem profissional ativo"
    assert f">{MSG_SEM_PROFISSIONAL_ATIVO}<" in admin.get(URL).text
    assert f"{MSG_SEM_PROFISSIONAL_ATIVO}. Quem tenta contratar" in admin.get("/admin/funcionarios").text


def test_valor_em_lora_botao_alterar_valor_e_tom_neutro(db, admin):
    fono = criar_especialidade(db, "Fonoaudiologia", "1250.50")
    criar_especialidade(db, "Terapia Ocupacional", "160.00")
    html = admin.get(URL).text
    linha = _linha(html, "Fonoaudiologia")
    assert 'class="text-end esp-valor font-monospace-num">R$ 1.250,50<' in linha
    assert f'href="/admin/especialidades/{fono.id}/editar"' in linha and "Alterar valor" in linha
    assert ">Editar<" not in html
    assert 'class="chip-especialidade esp-neutra">Terapia Ocupacional<' in html
    assert 'class="chip-especialidade esp-fonoaudiologia">Fonoaudiologia<' in html


def test_lista_em_ordem_alfabetica(db, admin):
    for nome in ("Psicologia", "Fonoaudiologia", "Musicoterapia"):
        criar_especialidade(db, nome, "100.00")
    html = admin.get(URL).text
    posicoes = [html.index(f">{nome}</span>") for nome in ("Fonoaudiologia", "Musicoterapia", "Psicologia")]
    assert posicoes == sorted(posicoes)


# --- Descontos -----------------------------------------------------------------------------------

def test_bloco_de_descontos_somente_leitura(db, admin):
    criar_especialidade(db, "Fonoaudiologia", "150.00")
    html = " ".join(admin.get(URL).text.split())
    assert "Descontos por duração do plano" in html
    assert "Valem para todas as especialidades e são definidos pelo sistema. Não dá para alterá-los aqui." in html
    for duracao, percentual in (("quinzenal", 0), ("mensal", 5), ("bimestral", 7), ("anual", 10)):
        assert f"<dt>{duracao}</dt> <dd>{percentual}%</dd>" in html
    bloco = html[html.index('class="esp-descontos"'):]
    bloco = bloco[:bloco.index("</section>")]
    assert "<input" not in bloco and "<form" not in bloco and "<a " not in bloco


def test_descontos_vem_da_constante(db, admin, monkeypatch):
    criar_especialidade(db, "Fonoaudiologia", "150.00")
    monkeypatch.setitem(templating.templates.env.globals, "descontos_por_duracao", {"mensal": 6, "anual": 12})
    html = " ".join(admin.get(URL).text.split())
    assert "<dt>mensal</dt> <dd>6%</dd>" in html and "<dt>anual</dt> <dd>12%</dd>" in html
    assert "quinzenal" not in html and "<dd>10%</dd>" not in html


def test_global_dos_descontos_e_a_propria_constante():
    assert templating.templates.env.globals["descontos_por_duracao"] is DESCONTO_POR_DURACAO


# --- Lista vazia ---------------------------------------------------------------------------------

def test_lista_vazia(admin):
    html = " ".join(admin.get(URL).text.split())
    assert "Nenhuma especialidade cadastrada" in html
    assert "Cadastre a primeira e o valor da sessão. Depois, cadastre um profissional para ela" in html
    assert html.count('href="/admin/especialidades/nova"') == 1 and "Cadastrar especialidade" in html
    assert "Descontos por duração do plano" not in html and "<table" not in html


# --- Nova especialidade --------------------------------------------------------------------------

def test_formulario_novo_tem_placeholder_dica_e_nota(admin):
    html = " ".join(admin.get(f"{URL}/nova").text.split())
    assert 'placeholder="Ex.: Neuropsicologia"' in html
    assert 'id="valor_sessao-ajuda">Use vírgula para os centavos. Ex.: 150,00.</p>' in html
    assert 'aria-describedby="valor_sessao-ajuda"' in html
    assert ("<strong>Depois de cadastrar</strong>Cadastre um profissional para essa especialidade. "
            "Sem profissional ativo, ninguém consegue contratar.") in html
    assert "cada sessão de 60 minutos" in html
    assert 'aria-invalid' not in html


@pytest.mark.parametrize("nome", ["fonoaudiologia", "FONOAUDIOLOGIA", "  Fonoaudiologia "])
def test_nome_repetido_erro_no_campo(db, admin, nome):
    criar_especialidade(db, "Fonoaudiologia", "150.00")
    resposta = admin.post(f"{URL}/nova", data={"nome": nome, "valor_sessao": "170,00"})
    assert resposta.status_code == 400
    html = " ".join(resposta.text.split())
    assert MSG_ESPECIALIDADE_REPETIDA == "Já existe uma especialidade com esse nome."
    campo = html[html.index('id="nome"'):html.index('id="valor_sessao"')]
    assert 'is-invalid' in campo and 'aria-invalid="true"' in campo and 'aria-describedby="nome-erro"' in campo
    assert f'id="nome-erro"><i class="bi bi-exclamation-circle me-1" aria-hidden="true"></i>{MSG_ESPECIALIDADE_REPETIDA}</p>' in campo
    assert 'value="170,00"' in html  # o valor digitado não se perde
    assert db.query(Especialidade).count() == 1


# --- Alterar valor -------------------------------------------------------------------------------

def test_alterar_valor_mostra_chip_valor_atual_e_nota(db, admin):
    pedag = criar_especialidade(db, "Psicopedagogia", "180.00")
    html = " ".join(admin.get(f"{URL}/{pedag.id}/editar").text.split())
    assert "Alterar valor da sessão" in html and "Atualize o valor cobrado por sessão em Psicopedagogia." in html
    assert 'class="chip-especialidade esp-psicopedagogia">Psicopedagogia<' in html
    assert '<span class="esp-valor esp-valor-atual">R$ 180,00</span>' in html
    assert 'value="180,00"' in html  # leitura do valor atual mantida
    assert ("<strong>Quem é afetado</strong>O novo valor vale para novos planos e renovações. "
            "Planos já contratados mantêm o valor cobrado na contratação.") in html
    assert 'name="nome"' not in html  # o nome continua sem edição


@pytest.mark.parametrize("valor, mensagem", [
    ("0", "Informe um valor maior que zero. Ex.: 180,00."),
    ("0,00", "Informe um valor maior que zero. Ex.: 180,00."),
    ("-5", "Informe um valor maior que zero. Ex.: 180,00."),
    ("abc", "Informe um valor numérico, como 150,00."),
    ("", "Este campo é obrigatório."),
    ("   ", "Este campo é obrigatório."),
])
def test_alterar_valor_invalido_erro_no_campo(db, admin, valor, mensagem):
    pedag = criar_especialidade(db, "Psicopedagogia", "180.00")
    resposta = admin.post(f"{URL}/{pedag.id}/editar", data={"valor_sessao": valor})
    assert resposta.status_code == 400
    html = " ".join(resposta.text.split())
    assert 'aria-invalid="true"' in html and 'aria-describedby="valor_sessao-erro"' in html
    assert 'class="form-control is-invalid"' in html
    assert f'id="valor_sessao-erro"><i class="bi bi-exclamation-circle me-1" aria-hidden="true"></i>{mensagem}</p>' in html
    assert '<span class="esp-valor esp-valor-atual">R$ 180,00</span>' in html  # valor atual continua no topo
    db.refresh(pedag)
    assert pedag.valor_sessao == Decimal("180.00")


@pytest.mark.parametrize("valor", ["0", "-5", "abc", ""])
def test_nova_especialidade_valor_invalido_erro_no_campo(db, admin, valor):
    resposta = admin.post(f"{URL}/nova", data={"nome": "Neuropsicologia", "valor_sessao": valor})
    assert resposta.status_code == 400
    html = " ".join(resposta.text.split())
    assert 'aria-describedby="valor_sessao-erro valor_sessao-ajuda"' in html
    assert 'id="valor_sessao-erro">' in html
    assert db.query(Especialidade).count() == 0


def test_alterar_valor_nao_muda_plano_ja_contratado(db, admin):
    fono = criar_especialidade(db, "Fonoaudiologia", "150.00")
    prof = criar_funcionario(db, fono)
    plano = criar_plano_com_sessao(db, prof.usuario.id, fono, StatusSessao.AGENDADA, valor_total="1200.00")
    assert admin.post(f"{URL}/{fono.id}/editar", data={"valor_sessao": "175,00"}, follow_redirects=False).status_code == 303
    db.expire_all()
    assert db.get(Plano, plano.id).valor_total == Decimal("1200.00")


@pytest.mark.parametrize("ident", ["abc", "0", "-1", "²", "99999999999999999999", "999"])
def test_alterar_valor_id_invalido_ou_inexistente_404(admin, ident):
    assert admin.get(f"{URL}/{ident}/editar").status_code == 404


@pytest.mark.parametrize("perfil", ["cliente", "funcionario"])
def test_alterar_valor_bloqueado_para_outros_perfis(client, db, perfil):
    fono = criar_especialidade(db, "Fonoaudiologia", "150.00")
    usuario = criar_cliente(db) if perfil == "cliente" else criar_funcionario(db, fono).usuario
    logar(client, usuario.email)
    assert client.get(f"{URL}/{fono.id}/editar").status_code == 403


# --- SQL -----------------------------------------------------------------------------------------

def _contar_sql(client, db, url) -> int:
    comandos = []
    ouvinte = lambda *args: comandos.append(args[2])  # noqa: E731
    event.listen(db.get_bind(), "before_cursor_execute", ouvinte)
    try:
        assert client.get(url).status_code == 200
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", ouvinte)
    return len(comandos)


def test_lista_faz_uma_consulta_com_poucas_e_muitas_especialidades(db, admin):
    _prof(db, criar_especialidade(db, "Fonoaudiologia", "150.00"), "Carla Lima")
    _contar_sql(admin, db, URL)  # aquece: a 1ª requisição recarrega o usuário logado
    poucas = _contar_sql(admin, db, URL)
    for i in range(15):
        especialidade = criar_especialidade(db, f"Especialidade {i:02d}", "100.00")
        for j in range(i % 4):
            _prof(db, especialidade, f"P{i:02d}{j} Nome", desativado=(j == 2))
    db.expire_all()
    _contar_sql(admin, db, URL)
    assert _contar_sql(admin, db, URL) == poucas == 1
