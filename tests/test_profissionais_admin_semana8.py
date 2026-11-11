import re
from datetime import datetime, timedelta

import pytest
from sqlalchemy import event, select

from app.core.templating import MSG_SEM_PROFISSIONAIS
from app.db.base import Base
from app.models.funcionario import Funcionario
from app.models.sessao import StatusSessao
from app.routers import admin as admin_router
from app.routers.admin import MSG_ESPECIALIDADE_COM_SESSOES, MSG_FUNCIONARIO_COM_SESSOES, titulo_do_resumo_de_erros
from app.services import funcionario_service
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

AGORA = datetime(2026, 11, 10, 10, 0)  # horário local
FUTURA = datetime(2026, 11, 18, 9, 0)
PASSADA = datetime(2026, 11, 3, 9, 0)
SENHA_NOVA = "SenhaForte123"


@pytest.fixture(autouse=True)
def relogio_fixo(monkeypatch):
    monkeypatch.setattr(admin_router, "agora_local", lambda: AGORA)


@pytest.fixture
def admin(client, db):
    logar(client, criar_admin(db).email)
    return client


@pytest.fixture
def fono(db):
    return criar_especialidade(db, "Fonoaudiologia", "150.00")


@pytest.fixture
def pedag(db):
    return criar_especialidade(db, "Psicopedagogia", "180.00")


@pytest.fixture
def paciente(db):
    return criar_paciente(db, criar_cliente(db).id)


def _prof(db, especialidade, nome, desativado=False):
    resumo = criar_funcionario(db, especialidade, email=f"{nome.split()[0].lower()}@exemplo.com", nome=nome)
    if desativado:
        funcionario_service.desativar(db, resumo.usuario.id, AGORA)
    return resumo


def _sessoes(db, paciente, prof, sessoes):
    return criar_plano(db, paciente, prof, sessoes)


def _html(resposta) -> str:
    return " ".join(resposta.text.split())


def _grupo(html: str, nome: str) -> str:
    return re.search(rf'<section class="prof-grupo[^"]*" aria-labelledby="grupo-\d+">\s*<header[^>]*>\s*<h2[^>]*>{nome}</h2>.*?</section>', html).group(0)


def _linha(html: str, nome: str) -> str:
    return re.search(rf'<li class="prof-linha[^"]*">(?:(?!</li>).)*?<strong>{nome}</strong>.*?</li>', html).group(0)


# --- Contagem de consultas futuras (mesma regra da RN05) -------------------------------------------

@pytest.mark.parametrize("status, quando, conta", [
    (AGENDADA, FUTURA, True),
    (PENDENTE, FUTURA, True),
    (REALIZADA, FUTURA, False),
    (AGENDADA, PASSADA, False),
    (PENDENTE, PASSADA, False),
    (AGENDADA, AGORA, True),  # data_hora >= agora, como na RN05
    (AGENDADA, AGORA - timedelta(minutes=1), False),
])
def test_contagem_de_consultas_futuras(db, fono, paciente, status, quando, conta):
    carla = _prof(db, fono, "Carla Lima")
    _sessoes(db, paciente, carla, [(quando, status)])
    assert funcionario_service.contar_sessoes_futuras(db, carla.usuario.id, AGORA) == int(conta)
    [grupo] = funcionario_service.listar_por_especialidade(db, AGORA)
    assert grupo.profissionais[0].consultas_futuras == int(conta)


def test_contagem_soma_varias_e_ignora_outro_profissional(db, fono, paciente):
    carla, rui = _prof(db, fono, "Carla Lima"), _prof(db, fono, "Rui Costa")
    _sessoes(db, paciente, carla, [(FUTURA, AGENDADA), (FUTURA + timedelta(days=7), PENDENTE),
                                   (FUTURA + timedelta(days=14), AGENDADA), (PASSADA, REALIZADA)])
    _sessoes(db, paciente, rui, [(FUTURA, AGENDADA)])
    contagens = {p.nome: p.consultas_futuras for p in funcionario_service.listar_por_especialidade(db, AGORA)[0].profissionais}
    assert contagens == {"Carla Lima": 3, "Rui Costa": 1}


def test_lista_e_409_concordam(db, client, admin, fono, pedag, paciente):
    cenarios = {
        "Ana Agendada": [(FUTURA, AGENDADA)],
        "Bia Pendente": [(FUTURA, PENDENTE)],
        "Caio Realizada": [(FUTURA, REALIZADA)],
        "Davi Passada": [(PASSADA, AGENDADA), (PASSADA, PENDENTE)],
        "Eva Agora": [(AGORA, AGENDADA)],
        "Fabio Livre": [],
    }
    for i, (nome, sessoes) in enumerate(cenarios.items()):
        prof = _prof(db, fono if i % 2 else pedag, nome)
        if sessoes:
            _sessoes(db, paciente, prof, sessoes)
    lista = {p.nome: (p.id, p.consultas_futuras > 0)
             for g in funcionario_service.listar_por_especialidade(db, AGORA) for p in g.profissionais}
    html = _html(client.get("/admin/funcionarios"))
    resultado = {}
    for nome, (id_, tem_futuras) in lista.items():
        bloqueado_na_tela = 'aria-disabled="true"' in _linha(html, nome)
        resposta = client.post(f"/admin/funcionarios/{id_}/desativar", follow_redirects=False)
        resultado[nome] = (tem_futuras, bloqueado_na_tela, resposta.status_code == 409)
    assert all(a == b == c for a, b, c in resultado.values()), resultado
    assert sorted(n for n, (t, _, _) in resultado.items() if t) == ["Ana Agendada", "Bia Pendente", "Eva Agora"]


# --- Lista agrupada --------------------------------------------------------------------------------------

def test_grupos_em_ordem_alfabetica_ativos_por_nome_e_depois_desativados(db, admin, fono, pedag):
    criar_especialidade(db, "Terapia Ocupacional", "200.00")
    _prof(db, pedag, "Rafael Costa")
    _prof(db, pedag, "Juliana Martins")
    _prof(db, pedag, "Aline Souza", desativado=True)
    _prof(db, fono, "Carlos Lima")
    html = _html(admin.get("/admin/funcionarios"))
    assert re.findall(r'class="titulo-serifa prof-grupo-titulo" id="grupo-\d+">([^<]+)<', html) == [
        "Fonoaudiologia", "Psicopedagogia", "Terapia Ocupacional"]
    assert re.findall(r"<strong>([^<]+)</strong>", _grupo(html, "Psicopedagogia")) == [
        "Juliana Martins", "Rafael Costa", "Aline Souza"]
    assert '<span class="prof-grupo-qtd">2 ativos, 1 desativado</span>' in _grupo(html, "Psicopedagogia")
    assert '<span class="prof-grupo-qtd">1 ativo</span>' in _grupo(html, "Fonoaudiologia")
    assert "3 ativos e 1 desativado, agrupados por especialidade." in html


def test_desativar_habilitado_desabilitado_e_reativar(db, admin, fono, paciente):
    livre, ocupada = _prof(db, fono, "Livre Lima"), _prof(db, fono, "Ocupada Souza")
    parado = _prof(db, fono, "Parado Rocha", desativado=True)
    _sessoes(db, paciente, ocupada, [(FUTURA, AGENDADA)] * 1 + [(FUTURA + timedelta(days=7), PENDENTE)])
    html = _html(admin.get("/admin/funcionarios"))

    linha = _linha(html, "Livre Lima")
    assert f'<a href="/admin/funcionarios/{livre.usuario.id}/desativar" class="prof-desativar">Desativar' in linha
    assert '>0</span> consultas futuras' in linha and "aria-disabled" not in linha

    linha = _linha(html, "Ocupada Souza")
    assert '<a class="prof-desativar prof-desativar-bloqueado" role="link" aria-disabled="true" aria-describedby="regra-desativar">' in linha
    assert "/desativar" not in linha and '>2</span> consultas futuras' in linha

    linha = _linha(html, "Parado Rocha")
    assert ">Desativado</span>" in linha and "prof-linha-desativada" in linha
    assert f'action="/admin/funcionarios/{parado.usuario.id}/reativar"' in linha and "<button" in linha
    assert "Desativar" not in linha.replace(">Desativado<", "")

    for linha in (_linha(html, "Livre Lima"), _linha(html, "Parado Rocha")):
        assert 'class="btn btn-outline-primary btn-sm">Editar' in linha
    assert '<p class="prof-nota" id="regra-desativar">Só é possível desativar quem não tem consultas futuras' in html


def test_singular_de_consulta_futura(db, admin, fono, paciente):
    _sessoes(db, paciente, _prof(db, fono, "Carla Lima"), [(FUTURA, AGENDADA)])
    assert '<span class="font-monospace-num">1</span> consulta futura</span>' in _html(admin.get("/admin/funcionarios"))


def test_especialidade_sem_ativo_mostra_a_frase_real_do_rf04(db, client, admin, fono, pedag):
    _prof(db, fono, "Carla Lima")
    _prof(db, pedag, "Aline Souza", desativado=True)
    criar_especialidade(db, "Psicologia", "170.00")  # nenhum profissional
    html = _html(admin.get("/admin/funcionarios"))
    aviso = f"Sem profissional ativo. Quem tenta contratar essa especialidade vê “{MSG_SEM_PROFISSIONAIS}”."
    assert aviso in _grupo(html, "Psicopedagogia") and aviso in _grupo(html, "Psicologia")
    assert "Sem profissional ativo" not in _grupo(html, "Fonoaudiologia")

    client.cookies.clear()
    logar(client, criar_cliente(db, email="cli@exemplo.com").email)
    assert MSG_SEM_PROFISSIONAIS in _html(client.get("/especialidades"))  # a mesma frase que o Cliente vê


def test_cores_de_especialidade_e_tom_neutro(db, admin, fono):
    outra = criar_especialidade(db, "Terapia Ocupacional", "200.00")
    _prof(db, fono, "Carla Lima")
    _prof(db, outra, "Tais Prado")
    html = _html(admin.get("/admin/funcionarios"))
    assert '<section class="prof-grupo esp-fonoaudiologia"' in html
    assert '<section class="prof-grupo esp-neutra"' in html


def test_lista_vazia(db, admin, fono):
    html = _html(admin.get("/admin/funcionarios"))
    assert "Nenhum profissional cadastrado" in html
    assert 'href="/admin/funcionarios/novo" class="btn btn-primary btn-sm"' in html
    assert "prof-grupo" not in html and "Quem atende na clínica." in html


# --- Confirmação antes de desativar -------------------------------------------------------------------------

def _foto_do_banco(db) -> dict:
    db.expire_all()
    return {tabela.name: sorted(map(tuple, db.execute(select(tabela)).all()), key=repr) for tabela in Base.metadata.sorted_tables}


def test_confirmacao_nao_grava_nada(db, admin, fono, paciente):
    carla = _prof(db, fono, "Carla Lima")
    _prof(db, fono, "Rui Costa")
    _sessoes(db, paciente, carla, [(PASSADA, REALIZADA)])
    antes = _foto_do_banco(db)
    comandos = []
    ouvinte = lambda *args: comandos.append(args[2])  # noqa: E731
    event.listen(db.get_bind(), "before_cursor_execute", ouvinte)
    try:
        resposta = admin.get(f"/admin/funcionarios/{carla.usuario.id}/desativar")
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", ouvinte)
    assert resposta.status_code == 200
    assert _foto_do_banco(db) == antes
    assert not [c for c in comandos if re.match(r"\s*(INSERT|UPDATE|DELETE)", c, re.I)]


def test_conteudo_da_confirmacao(db, admin, fono):
    carla = _prof(db, fono, "Carla Lima")
    _prof(db, fono, "Rui Costa")
    html = _html(admin.get(f"/admin/funcionarios/{carla.usuario.id}/desativar"))
    assert "Desativar Carla Lima?" in html and "Nada foi alterado ainda." in html
    assert 'class="chip-especialidade esp-fonoaudiologia">Fonoaudiologia<' in html and "0 consultas futuras" in html
    for frase in ("Carla Lima deixa de aparecer para quem vai contratar um plano.",
                  "Planos encerrados e o histórico de consultas continuam como estão.",
                  "Carla Lima ainda consegue entrar no sistema e ver a própria agenda.",
                  "Você pode reativar quando quiser, na lista de Profissionais."):
        assert frase in html
    form = re.search(r"<form method=\"post\" action=\"([^\"]+)\" class=\"prof-confirmar-acoes\">(.*?)</form>", html)
    assert form.group(1) == f"/admin/funcionarios/{carla.usuario.id}/desativar"
    assert '<button type="submit" class="btn btn-danger">Desativar Carla Lima</button>' in form.group(2)
    assert "<input" not in form.group(2)  # o POST de sempre, sem campo nenhum
    assert '<a href="/admin/funcionarios" class="btn btn-link text-decoration-none">Cancelar</a>' in form.group(2)
    assert "fica sem profissional ativo" not in html


def test_aviso_de_ultimo_ativo(db, admin, fono):
    carla = _prof(db, fono, "Carla Lima")
    _prof(db, fono, "Aline Souza", desativado=True)  # desativado não conta
    html = _html(admin.get(f"/admin/funcionarios/{carla.usuario.id}/desativar"))
    assert ("<strong>Fonoaudiologia fica sem profissional ativo.</strong> Enquanto isso, quem tenta contratar essa "
            f"especialidade vê “{MSG_SEM_PROFISSIONAIS}”.") in html


def test_confirmacao_com_consultas_futuras_redireciona_com_a_mensagem_da_rn05(db, admin, fono, paciente):
    carla = _prof(db, fono, "Carla Lima")
    _sessoes(db, paciente, carla, [(FUTURA, PENDENTE)])
    resposta = admin.get(f"/admin/funcionarios/{carla.usuario.id}/desativar", follow_redirects=False)
    assert resposta.status_code == 303
    assert resposta.headers["location"] == f"/admin/funcionarios?erro=sessoes-futuras&profissional={carla.usuario.id}"
    html = _html(admin.get(resposta.headers["location"]))
    assert MSG_FUNCIONARIO_COM_SESSOES.format(nome="Carla Lima") in html and 'role="alert"' in html
    assert db.get(Funcionario, carla.usuario.id).ativo is True


@pytest.mark.parametrize("consulta", ["erro=outra&profissional={id}", "erro=sessoes-futuras&profissional=9999",
                                      "erro=sessoes-futuras&profissional=abc", "erro=sessoes-futuras"])
def test_mensagem_da_lista_so_com_chave_e_profissional_validos(db, admin, fono, consulta):
    carla = _prof(db, fono, "Carla Lima")
    html = admin.get("/admin/funcionarios?" + consulta.format(id=carla.usuario.id)).text
    assert "Não é possível desativar" not in html and 'role="alert"' not in html


@pytest.mark.parametrize("valor", ["9999", "abc", "0", "-1", "²", "2147483648", "9" * 30])
def test_confirmacao_inexistente_ou_invalido_da_404(db, admin, fono, valor):
    assert admin.get(f"/admin/funcionarios/{valor}/desativar").status_code == 404


def test_confirmacao_de_ja_desativado_da_404(db, admin, fono):
    aline = _prof(db, fono, "Aline Souza", desativado=True)
    assert admin.get(f"/admin/funcionarios/{aline.usuario.id}/desativar").status_code == 404


@pytest.mark.parametrize("perfil", ["cliente", "funcionario"])
def test_confirmacao_so_para_admin(db, client, fono, perfil):
    carla = _prof(db, fono, "Carla Lima")
    email = criar_cliente(db, email="c@exemplo.com").email if perfil == "cliente" else carla.usuario.email
    logar(client, email)
    assert client.get(f"/admin/funcionarios/{carla.usuario.id}/desativar").status_code == 403


def test_post_de_desativacao_continua_igual(db, admin, fono, paciente):
    livre, ocupada = _prof(db, fono, "Livre Lima"), _prof(db, fono, "Ocupada Souza")
    _sessoes(db, paciente, ocupada, [(FUTURA, AGENDADA)])
    resposta = admin.post(f"/admin/funcionarios/{livre.usuario.id}/desativar", follow_redirects=False)
    assert resposta.status_code == 303 and resposta.headers["location"] == "/admin/funcionarios?ok=desativado"
    resposta = admin.post(f"/admin/funcionarios/{ocupada.usuario.id}/desativar", follow_redirects=False)
    assert resposta.status_code == 409 and MSG_FUNCIONARIO_COM_SESSOES.format(nome="Ocupada Souza") in resposta.text
    db.expire_all()
    assert db.get(Funcionario, livre.usuario.id).ativo is False and db.get(Funcionario, ocupada.usuario.id).ativo is True


# --- Formulário: erros por campo e resumo ------------------------------------------------------------------

def _novo(admin, fono, **campos):
    dados = {"nome": "Marina Duarte", "email": "marina@exemplo.com", "especialidade_id": fono.id, "senha": SENHA_NOVA, **campos}
    return admin.post("/admin/funcionarios/novo", data=dados)


@pytest.mark.parametrize("quantidade, editando, esperado", [
    (1, False, "Revise 1 campo antes de cadastrar."), (2, False, "Revise 2 campos antes de cadastrar."),
    (1, True, "Revise 1 campo antes de salvar."), (3, True, "Revise 3 campos antes de salvar."),
])
def test_titulo_do_resumo_de_erros(quantidade, editando, esperado):
    assert titulo_do_resumo_de_erros(quantidade, editando) == esperado


def test_erros_no_campo_e_resumo_no_topo(db, admin, fono):
    resposta = _novo(admin, fono, email="sem-arroba", senha="Zq7xw")
    html = _html(resposta)
    assert resposta.status_code == 400
    resumo = re.search(r'<div class="alert alert-danger prof-form-erros" role="alert">.*?</div>', html).group(0)
    assert "Revise 2 campos antes de cadastrar." in resumo
    assert re.findall(r'<li><a href="#(\w+)">([^<]+)</a>', resumo) == [("email", "E-mail de acesso"), ("senha", "Senha provisória")]
    assert "A senha deve ter pelo menos 8 caracteres." in resumo
    for nome in ("email", "senha"):
        campo = re.search(rf'<input type="\w+" id="{nome}".*?>', html).group(0)
        assert "is-invalid" in campo and 'aria-invalid="true"' in campo and f"{nome}-erro" in campo
        assert re.search(rf'<p class="invalid-feedback d-block prof-campo-erro" id="{nome}-erro"><i class="bi bi-exclamation-circle', html)
    assert 'aria-invalid' not in re.search(r'<input type="text" id="nome".*?>', html).group(0)
    assert "Zq7xw" not in html  # a senha digitada nunca volta na página


def test_email_repetido_aparece_no_campo_e_no_resumo(db, admin, fono):
    _prof(db, fono, "Juliana Martins")
    html = _html(_novo(admin, fono, email="juliana@exemplo.com"))
    assert "Revise 1 campo antes de cadastrar." in html
    assert '<li><a href="#email">E-mail de acesso</a>: Já existe uma conta com este e-mail.</li>' in html
    assert re.search(r'id="email-erro"><i class="bi bi-exclamation-circle me-1" aria-hidden="true"></i>Já existe uma conta', html)


def test_um_erro_no_singular(db, admin, fono):
    html = _html(_novo(admin, fono, telefone_contato="telefone"))
    assert "Revise 1 campo antes de cadastrar." in html and "Informe um telefone válido, com DDD." in html


def test_resumo_na_edicao_diz_salvar(db, admin, fono):
    carla = _prof(db, fono, "Carla Lima")
    resposta = admin.post(f"/admin/funcionarios/{carla.usuario.id}/editar",
                          data={"nome": "Carla Lima", "especialidade_id": fono.id, "email_contato": "sem-arroba"})
    assert resposta.status_code == 400 and "Revise 1 campo antes de salvar." in _html(resposta)


def test_sem_erro_sem_resumo_e_campos_da_parte_opcional(db, admin, fono):
    html = _html(admin.get("/admin/funcionarios/novo"))
    assert "prof-form-erros" not in html and "(opcional)" not in html
    assert "Acesso ao sistema" in html and "Perfil que o cliente vê" in html
    assert "Todos os campos desta parte são opcionais." in html
    assert re.search(r'<textarea id="formacao" name="formacao" rows="2"[^>]*maxlength="300"', html)
    assert "Mínimo de 8 caracteres. Anote antes de salvar: depois não dá para ver." in html
    assert "Como o cliente vê" not in html  # o cartão só existe na edição


def test_editar_mostra_o_email_de_login_como_texto(db, admin, fono):
    carla = _prof(db, fono, "Carla Lima")
    html = _html(admin.get(f"/admin/funcionarios/{carla.usuario.id}/editar"))
    assert '<p class="prof-campo-fixo">carla@exemplo.com</p>' in html and "Não pode ser alterado." in html
    assert 'name="email"' not in html and 'name="senha"' not in html


# --- RN07: especialidade travada ---------------------------------------------------------------------------------

def test_especialidade_travada_com_consultas_futuras(db, admin, fono, pedag, paciente):
    juliana = _prof(db, pedag, "Juliana Martins")
    _sessoes(db, paciente, juliana, [(FUTURA + timedelta(days=7 * i), AGENDADA) for i in range(5)] + [(FUTURA, PENDENTE)])
    html = _html(admin.get(f"/admin/funcionarios/{juliana.usuario.id}/editar"))
    select_ = re.search(r'<select id="especialidade_id".*?</select>', html).group(0)
    assert " disabled " in select_ and 'name="especialidade_id"' not in select_
    assert 'aria-describedby="especialidade_id-trava"' in select_
    assert re.search(rf'<option value="{pedag.id}" selected>', select_)
    assert f'<input type="hidden" name="especialidade_id" value="{pedag.id}">' in html
    assert ('<p class="prof-trava" id="especialidade_id-trava">Juliana tem 6 consultas futuras. '
            "Só dá para trocar a especialidade quando não houver nenhuma.</p>") in html


def test_especialidade_travada_no_singular_e_livre_sem_consultas(db, admin, fono, pedag, paciente):
    juliana, rafael = _prof(db, pedag, "Juliana Martins"), _prof(db, pedag, "Rafael Costa")
    _sessoes(db, paciente, juliana, [(FUTURA, AGENDADA)])
    assert "Juliana tem 1 consulta futura." in _html(admin.get(f"/admin/funcionarios/{juliana.usuario.id}/editar"))
    html = _html(admin.get(f"/admin/funcionarios/{rafael.usuario.id}/editar"))
    select_ = re.search(r'<select id="especialidade_id".*?</select>', html).group(0)
    assert "disabled" not in select_ and 'name="especialidade_id"' in select_
    assert 'type="hidden" name="especialidade_id"' not in html and "prof-trava" not in html


def test_servidor_continua_recusando_a_troca(db, admin, fono, pedag, paciente):
    juliana = _prof(db, pedag, "Juliana Martins")
    _sessoes(db, paciente, juliana, [(FUTURA, AGENDADA)])
    url = f"/admin/funcionarios/{juliana.usuario.id}/editar"
    resposta = admin.post(url, data={"nome": "Juliana Martins", "especialidade_id": fono.id})
    assert resposta.status_code == 409
    assert MSG_ESPECIALIDADE_COM_SESSOES.format(nome="Juliana Martins") in resposta.text
    db.expire_all()
    assert db.get(Funcionario, juliana.usuario.id).especialidade_id == pedag.id
    # o valor do campo oculto (a mesma especialidade) salva normalmente
    resposta = admin.post(url, data={"nome": "Juliana M. Martins", "especialidade_id": pedag.id}, follow_redirects=False)
    assert resposta.status_code == 303


# --- Cartão "Como o cliente vê" ------------------------------------------------------------------------------------

def _cartao(html: str) -> str:
    return re.search(r'<aside class="cartao prof-form-parte prof-form-previa.*?</aside>', html).group(0)


@pytest.fixture
def juliana_completa(db, pedag):
    juliana = _prof(db, pedag, "Juliana Martins")
    funcionario = db.get(Funcionario, juliana.usuario.id)
    funcionario.formacao = "Pedagogia (UPF)"
    funcionario.registro_profissional = "ABPp 1234/RS"
    funcionario.apresentacao = "Atendo crianças com dificuldades de leitura."
    funcionario.telefone_contato = "(54) 99812-3344"
    funcionario.email_contato = "contato.juliana@clinica.com"
    db.commit()
    return juliana


def test_cartao_nunca_tem_o_email_de_login(db, admin, juliana_completa, pedag):
    url = f"/admin/funcionarios/{juliana_completa.usuario.id}/editar"
    email_login = juliana_completa.usuario.email
    cartoes = [_cartao(_html(admin.get(url)))]
    funcionario = db.get(Funcionario, juliana_completa.usuario.id)
    funcionario.email_contato = None
    db.commit()
    cartoes.append(_cartao(_html(admin.get(url))))
    cartoes.append(_cartao(_html(admin.post(url, data={"nome": "Juliana Martins", "especialidade_id": pedag.id,
                                                       "email_contato": email_login, "telefone_contato": "x"}))))
    for cartao in cartoes:
        assert email_login not in cartao
    assert "contato.juliana@clinica.com" in cartoes[0]
    assert not hasattr(funcionario_service.perfil_publico(juliana_completa.usuario, funcionario), "email")


def test_cartao_mostra_o_salvo_e_so_os_campos_preenchidos(db, admin, juliana_completa, pedag):
    url = f"/admin/funcionarios/{juliana_completa.usuario.id}/editar"
    cartao = _cartao(_html(admin.get(url)))
    for valor in ("Juliana Martins", "ABPp 1234/RS", "Pedagogia (UPF)", "Atendo crianças com dificuldades de leitura.",
                  "(54) 99812-3344", "contato.juliana@clinica.com"):
        assert valor in cartao
    assert "Contratar" not in cartao and "/contratar/" not in cartao
    assert "Reflete o que está salvo. Só aparecem os campos preenchidos." in cartao
    # erro de validação: o cartão continua com o que está salvo, não com o que foi digitado
    resposta = admin.post(url, data={"nome": "Juliana Martins", "especialidade_id": pedag.id,
                                     "formacao": "Formação digitada agora", "telefone_contato": "telefone"})
    assert resposta.status_code == 400
    cartao = _cartao(_html(resposta))
    assert "Pedagogia (UPF)" in cartao and "Formação digitada agora" not in cartao

    vazia = _prof(db, pedag, "Vera Lopes")
    cartao = _cartao(_html(admin.get(f"/admin/funcionarios/{vazia.usuario.id}/editar")))
    assert "Vera Lopes" in cartao
    for classe in ("perfil-registro", "perfil-formacao", "perfil-apresentacao", "perfil-contato"):
        assert classe not in cartao


def test_apresentacao_com_html_sai_escapada(db, admin, pedag):
    juliana = _prof(db, pedag, "Juliana Martins")
    db.get(Funcionario, juliana.usuario.id).apresentacao = "<script>alert(1)</script><b>oi</b>"
    db.commit()
    html = admin.get(f"/admin/funcionarios/{juliana.usuario.id}/editar").text
    assert "<script>alert(1)</script>" not in html and "<b>oi</b>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;&lt;b&gt;oi&lt;/b&gt;" in _cartao(" ".join(html.split()))


def test_cartao_do_cliente_continua_com_o_botao_contratar(db, client, fono):
    carla = _prof(db, fono, "Carla Lima")
    logar(client, criar_cliente(db, email="cli@exemplo.com").email)
    html = _html(client.get(f"/especialidades/{fono.id}/profissionais"))
    assert f'href="/contratar/{carla.usuario.id}"' in html


# --- Sem N+1 ---------------------------------------------------------------------------------------------------------

def _contar_sql(client, db, url) -> int:
    comandos = []
    ouvinte = lambda *args: comandos.append(args[2])  # noqa: E731
    event.listen(db.get_bind(), "before_cursor_execute", ouvinte)
    try:
        assert client.get(url).status_code == 200
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", ouvinte)
    return len(comandos)


def test_lista_sem_n_mais_1(db, admin, fono, pedag, paciente):
    _sessoes(db, paciente, _prof(db, fono, "Carla Lima"), [(FUTURA, AGENDADA)])
    _contar_sql(admin, db, "/admin/funcionarios")  # aquece: a 1ª requisição recarrega o usuário logado
    poucos = _contar_sql(admin, db, "/admin/funcionarios")
    for i in range(12):
        especialidade = criar_especialidade(db, f"Especialidade {i:02d}", "100.00")
        for j in range(3):
            prof = _prof(db, especialidade, f"P{i:02d}{j} Nome", desativado=(j == 2))
            if j == 0:
                _sessoes(db, paciente, prof, [(FUTURA + timedelta(days=d), AGENDADA) for d in range(3)])
    db.expire_all()
    _contar_sql(admin, db, "/admin/funcionarios")
    assert _contar_sql(admin, db, "/admin/funcionarios") == poucos == 1
