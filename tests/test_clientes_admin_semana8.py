import re
from datetime import date, datetime, time

import pytest
from sqlalchemy import event

from app.core.templating import descrever_configuracao
from app.models.cliente import Cliente
from app.models.pagamento import FormaPagamento, Pagamento, StatusPagamento
from app.models.sessao import StatusSessao
from app.routers import admin as admin_router
from app.services import admin_clientes_service as servico
from app.services import meus_atendidos_service
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

AGORA = datetime(2026, 3, 10, 11, 0)  # terça, horário local
CRIADO_NO_PRAZO = datetime(2026, 3, 10, 13, 0)  # UTC = 10:00 local; Pix até 14:00 local
CPF = "529.982.247-25"
URL = "/admin/clientes"


@pytest.fixture(autouse=True)
def relogio_fixo(monkeypatch):
    monkeypatch.setattr(admin_router, "agora_local", lambda: AGORA)


@pytest.fixture
def admin(client, db):
    logar(client, criar_admin(db).email)
    return client


@pytest.fixture
def juliana(db):
    return criar_funcionario(db, criar_especialidade(db, "Psicopedagogia", "180.00"), email="ju@exemplo.com", nome="Juliana Martins")


@pytest.fixture
def carlos(db):
    return criar_funcionario(db, criar_especialidade(db, "Fonoaudiologia", "150.00"), email="ca@exemplo.com", nome="Carlos Lima")


def _html(resposta) -> str:
    assert resposta.status_code == 200
    return " ".join(resposta.text.split())


def _pagamento(db, plano, forma):
    db.add(Pagamento(plano_id=plano.id, forma_pagamento=forma, status=StatusPagamento.PENDENTE))
    db.commit()


def _clientes(db, quantidade, prefixo="Cliente"):
    return [criar_cliente(db, email=f"{prefixo.lower()}{i:02d}@exemplo.com", nome=f"{prefixo} {i:02d}") for i in range(quantidade)]


# --- Textos puros --------------------------------------------------------------------------------

@pytest.mark.parametrize("total, pagina, paginas, busca, esperado", [
    (1, 1, 1, "", "1 cliente"),
    (4, 1, 1, "", "4 clientes"),
    (1, 1, 1, "maria", "1 cliente encontrado"),
    (3, 1, 1, "souza", "3 clientes encontrados"),
    (0, 1, 1, "xyz", "0 clientes encontrados"),
    (47, 1, 3, "", "Clientes 1 a 20 de 47"),
    (47, 2, 3, "", "Clientes 21 a 40 de 47"),
    (47, 3, 3, "", "Clientes 41 a 47 de 47"),
    (25, 2, 2, "souza", "Clientes 21 a 25 de 25"),
])
def test_texto_contagem(total, pagina, paginas, busca, esperado):
    assert servico.texto_contagem(total, pagina, paginas, busca) == esperado


@pytest.mark.parametrize("atendidos, ativos, esperado", [
    (0, 0, "Nenhum atendido cadastrado."),
    (1, 0, "1 atendido e nenhum plano ativo."),
    (1, 1, "1 atendido e 1 plano ativo."),
    (2, 1, "2 atendidos e 1 plano ativo."),
    (2, 3, "2 atendidos e 3 planos ativos."),
])
def test_texto_resumo_do_cliente(atendidos, ativos, esperado):
    assert servico.texto_resumo_do_cliente(atendidos, ativos) == esperado


def test_texto_prazo_por_forma():
    assert servico.texto_prazo(CRIADO_NO_PRAZO, date(2026, 3, 19), None, AGORA) == "10/03/2026, 14:00"
    assert servico.texto_prazo(CRIADO_NO_PRAZO, date(2026, 3, 19), FormaPagamento.PIX, AGORA) == "10/03/2026, 14:00"
    # boleto: menor entre 3 dias úteis (13/03) e o 2º dia útil antes da 1ª sessão (17/03); só a data
    assert servico.texto_prazo(CRIADO_NO_PRAZO, date(2026, 3, 19), FormaPagamento.BOLETO, AGORA) == "13/03/2026"
    assert servico.texto_prazo(CRIADO_NO_PRAZO, date(2026, 3, 13), FormaPagamento.BOLETO, AGORA) == "11/03/2026"


def test_descrever_configuracao_continua_disponivel_em_meus_atendidos():
    assert meus_atendidos_service.descrever_configuracao is descrever_configuracao
    assert descrever_configuracao("bimestral", 2, [3, 1], time(15)) == "Bimestral, 2 vezes por semana, terças e quintas às 15:00"


# --- Lista ---------------------------------------------------------------------------------------

def test_lista_contagem_singular_e_plural(admin, db):
    criar_cliente(db)
    assert '<p class="meus-total" role="status">1 cliente</p>' in _html(admin.get(URL))
    _clientes(db, 3)
    assert '<p class="meus-total" role="status">4 clientes</p>' in _html(admin.get(URL))


def test_lista_busca_com_resultado_e_limpar(admin, db):
    criar_cliente(db, email="maria@exemplo.com", nome="Maria Souza")
    criar_cliente(db, email="joao@exemplo.com", nome="João Lima")
    html = _html(admin.get(URL, params={"q": "maria"}))
    assert ">1 cliente encontrado</p>" in html
    assert '<a href="/admin/clientes" class="cli-limpar">Limpar busca</a>' in html
    assert 'value="maria"' in html and "João Lima" not in html
    assert "Limpar busca" not in _html(admin.get(URL))


def test_lista_busca_sem_resultado(admin, db):
    criar_cliente(db)
    html = _html(admin.get(URL, params={"q": "xyz"}))
    assert "Nenhum cliente com “xyz”." in html
    assert "Confira a grafia ou busque só por uma parte do nome ou do e-mail." in html
    assert ">0 clientes encontrados</p>" in html and html.count("Limpar busca") == 2
    assert "cli-linha" not in html


def test_busca_sem_resultado_escapa_o_texto(admin, db):
    criar_cliente(db)
    html = admin.get(URL, params={"q": "<b>x</b>"}).text
    assert "<b>x</b>" not in html and "&lt;b&gt;x&lt;/b&gt;" in html


def test_lista_sem_clientes(admin, db):
    html = _html(admin.get(URL))
    assert "Nenhum cliente cadastrado" in html
    assert "Os clientes aparecem aqui quando criam a conta pela página de cadastro do site." in html
    assert 'role="search"' not in html and "meus-total" not in html


def test_lista_paginada_21_a_40_de_47(admin, db):
    _clientes(db, 47)
    html = _html(admin.get(URL, params={"pagina": "2"}))
    assert ">Clientes 21 a 40 de 47</p>" in html and "Página 2 de 3" in html
    assert 'href="/admin/clientes?pagina=1" rel="prev"' in html and 'href="/admin/clientes?pagina=3" rel="next"' in html
    assert "Cliente 20" in html and "Cliente 39" in html and "Cliente 19" not in html and "Cliente 40" not in html
    primeira = _html(admin.get(URL))
    assert ">Clientes 1 a 20 de 47</p>" in primeira and 'rel="prev"' not in primeira
    ultima = _html(admin.get(URL, params={"pagina": "3"}))
    assert ">Clientes 41 a 47 de 47</p>" in ultima and 'rel="next"' not in ultima


def test_paginacao_preserva_a_busca(admin, db):
    _clientes(db, 25, prefixo="Souza")
    html = _html(admin.get(URL, params={"q": "souza", "pagina": "2"}))
    assert ">Clientes 21 a 25 de 25</p>" in html
    assert 'href="/admin/clientes?q=souza&amp;pagina=1" rel="prev"' in html


def test_sem_paginacao_com_ate_20(admin, db):
    _clientes(db, 20)
    html = _html(admin.get(URL))
    assert ">20 clientes</p>" in html and "Página 1 de" not in html and "cli-paginacao" not in html


def test_linha_clicavel_nome_e_email(admin, db):
    cliente = criar_cliente(db, email="maria@exemplo.com", nome="Maria Souza")
    html = _html(admin.get(URL))
    linha = re.search(r'<li class="cli-linha">.*?</li>', html).group(0)
    assert f'<a href="/admin/clientes/{cliente.id}" class="meus-link">Maria Souza</a>' in linha
    assert '<span class="cli-email">maria@exemplo.com</span>' in linha
    assert linha.count("<a ") == 1  # só o link esticado: a linha inteira leva ao detalhe
    for rotulo in ("Telefone", "Cadastro", "Atendidos", "Planos ativos"):
        assert f'<span class="cli-rotulo">{rotulo}</span>' in linha


def test_cabecalho_centralizado_e_sem_coluna_de_aguardando(admin, db):
    criar_cliente(db)
    html = _html(admin.get(URL))
    for coluna in ("Telefone", "Cadastro", "Atendidos", "Planos ativos"):
        assert f'<span class="cli-centro">{coluna}</span>' in html
    assert "aguardando" not in html.lower()


def test_zeros_em_cinza(admin, db, carlos):
    sem = criar_cliente(db, email="sem@exemplo.com", nome="Ana Sem")
    com = criar_cliente(db, email="com@exemplo.com", nome="Bia Com")
    criar_plano(db, criar_paciente(db, com.id), carlos, [(datetime(2026, 3, 12, 9, 0), AGENDADA)])
    html = _html(admin.get(URL))
    linhas = re.findall(r'<li class="cli-linha">.*?</li>', html)
    linha_sem = next(l for l in linhas if f"/admin/clientes/{sem.id}" in l)
    linha_com = next(l for l in linhas if f"/admin/clientes/{com.id}" in l)
    assert linha_sem.count('class="font-monospace-num cli-zero">0</span>') == 2
    assert "cli-zero" not in linha_com.replace('<span class="cli-zero">—</span>', "")
    assert 'class="font-monospace-num ">1</span>' in linha_com


def test_planos_ativos_da_lista_seguem_a_regra_derivada(admin, db, carlos):
    cliente = criar_cliente(db)
    pedro = criar_paciente(db, cliente.id)
    criar_plano(db, pedro, carlos, [(datetime(2026, 3, 12, 9, 0), AGENDADA)])  # ativo
    criar_plano(db, pedro, carlos, [(datetime(2026, 3, 9, 9, 0), REALIZADA)])  # encerrado ontem
    criar_plano(db, pedro, carlos, [(datetime(2026, 3, 19, 9, 0), PENDENTE)], criado_em=CRIADO_NO_PRAZO)  # aguardando
    linha = re.search(r'<li class="cli-linha">.*?</li>', _html(admin.get(URL))).group(0)
    assert '<span class="cli-rotulo">Planos ativos</span> <span class="font-monospace-num ">1</span>' in linha
    detalhe = servico.detalhar(db, cliente.id, AGORA)
    assert detalhe.planos_ativos == servico.listar(db, AGORA).clientes[0].planos_ativos == 1


# --- Detalhe -------------------------------------------------------------------------------------

def _maria(db, juliana, carlos):
    cliente = criar_cliente(db, email="maria@exemplo.com", nome="Maria Souza")
    db.get(Cliente, cliente.id).cpf = CPF
    db.commit()
    pedro = criar_paciente(db, cliente.id, nome="Pedro Souza", nascimento=date(2017, 1, 5))
    laura = criar_paciente(db, cliente.id, nome="Laura Souza", nascimento=date(2020, 1, 5))
    ativo = criar_plano(db, pedro, juliana, [
        (datetime(2026, 3, 3, 15, 0), REALIZADA), (datetime(2026, 3, 5, 15, 0), REALIZADA),
        (datetime(2026, 3, 10, 15, 0), AGENDADA), (datetime(2026, 3, 12, 15, 0), AGENDADA),
    ], valor_total="2845.80")
    ativo.duracao, ativo.desconto_percentual = "bimestral", 7
    encerrado = criar_plano(db, laura, carlos, [(datetime(2026, 2, 4, 9, 0), REALIZADA)], valor_total="570.00")
    encerrado.desconto_percentual = 5
    aguardando = criar_plano(db, laura, carlos, [(datetime(2026, 3, 19, 13, 0), PENDENTE)],
                             criado_em=CRIADO_NO_PRAZO, valor_total="300.00")
    aguardando.duracao = "quinzenal"
    db.commit()
    return cliente, ativo, encerrado, aguardando


def test_detalhe_cabecalho_contato_e_resumo(admin, db, juliana, carlos):
    cliente, *_ = _maria(db, juliana, carlos)
    html = _html(admin.get(f"{URL}/{cliente.id}"))
    assert '<i class="bi bi-arrow-left me-1"></i>Clientes</a>' in html
    assert "Cliente desde 10/03/2026." in html
    assert "maria@exemplo.com" in html and "(54) 99999-1234" in html and "***.***.***-25" in html
    assert '<p class="cli-contato-resumo">2 atendidos e 1 plano ativo.</p>' in html
    assert "Pedro Souza</h2> <span>9 anos</span>" in html and "Laura Souza</h2> <span>6 anos</span>" in html


def test_detalhe_plano_ativo(admin, db, juliana, carlos):
    cliente, ativo, *_ = _maria(db, juliana, carlos)
    html = _html(admin.get(f"{URL}/{cliente.id}"))
    cartao = re.search(r'<article class="cli-plano esp-psicopedagogia".*?</article>', html).group(0)
    assert '<span class="chip-especialidade">Psicopedagogia</span> <span>com Juliana Martins</span>' in cartao
    assert '<span class="badge rounded-pill cli-selo-ativo">Ativo</span>' in cartao
    assert "Bimestral, 2 vezes por semana, terças e quintas às 15:00" in cartao
    assert ">03/03/2026</span> a <span class=\"font-monospace-num\">12/03/2026</span>" in cartao
    assert "2 de 4 consultas realizadas" in cartao and 'aria-valuenow="2"' in cartao and "width: 50.0%" in cartao
    assert "R$ 2.845,80</strong> <span>com 7% de desconto</span>" in cartao
    assert "Pagar até" not in cartao


def test_detalhe_aguardando_pagamento_sem_cobranca_usa_o_prazo_previsto(admin, db, juliana, carlos):
    cliente, *_ = _maria(db, juliana, carlos)
    html = _html(admin.get(f"{URL}/{cliente.id}"))
    cartao = re.search(r'<article class="cli-plano esp-fonoaudiologia"[^>]*>(?:(?!</article>).)*Aguardando pagamento.*?</article>', html).group(0)
    assert '<span class="badge rounded-pill status-pendente">Aguardando pagamento</span>' in cartao
    assert 'Pagar até <span class="font-monospace-num">10/03/2026, 14:00</span>' in cartao
    assert "Quinzenal, 1 vez por semana, quintas às 13:00" in cartao
    assert "R$ 300,00</strong> <span>sem desconto</span>" in cartao
    assert "consultas realizadas" not in cartao and "progressbar" not in cartao


@pytest.mark.parametrize("forma, prazo", [
    (FormaPagamento.PIX, "10/03/2026, 14:00"),
    (FormaPagamento.BOLETO, "13/03/2026"),
])
def test_detalhe_aguardando_pagamento_pix_e_boleto(admin, db, juliana, carlos, forma, prazo):
    cliente, _, _, aguardando = _maria(db, juliana, carlos)
    _pagamento(db, aguardando, forma)
    html = _html(admin.get(f"{URL}/{cliente.id}"))
    assert f'Pagar até <span class="font-monospace-num">{prazo}</span>' in html
    if forma == FormaPagamento.BOLETO:
        assert "13/03/2026," not in html  # boleto mostra só a data


def test_detalhe_encerrado_recolhido_e_por_ultimo(admin, db, juliana, carlos):
    cliente, *_ = _maria(db, juliana, carlos)
    html = _html(admin.get(f"{URL}/{cliente.id}"))
    laura = html[html.index("Laura Souza</h2>"):]
    assert laura.index("Aguardando pagamento") < laura.index('<details class="cli-encerrados">')
    detalhes = re.search(r'<details class="cli-encerrados">.*?</details>', laura).group(0)
    assert "<summary>1 plano encerrado</summary>" in detalhes
    assert '<span class="badge rounded-pill status-inativo">Encerrado</span>' in detalhes
    assert "1 de 1 consulta realizada" in detalhes and "R$ 570,00</strong> <span>com 5% de desconto</span>" in detalhes
    assert " open" not in detalhes.split(">")[0]
    assert "<details" not in html[:html.index("Laura Souza</h2>")]  # Pedro não tem encerrado


def test_detalhe_varios_encerrados_no_plural(admin, db, carlos):
    cliente = criar_cliente(db)
    pedro = criar_paciente(db, cliente.id)
    for dia in (2, 3):
        criar_plano(db, pedro, carlos, [(datetime(2026, 2, dia, 9, 0), REALIZADA)])
    html = _html(admin.get(f"{URL}/{cliente.id}"))
    assert "<summary>2 planos encerrados</summary>" in html
    assert '<p class="cli-contato-resumo">1 atendido e nenhum plano ativo.</p>' in html


def test_detalhe_cliente_sem_atendidos(admin, db):
    cliente = criar_cliente(db)
    html = _html(admin.get(f"{URL}/{cliente.id}"))
    assert "Ainda sem atendidos" in html
    assert "Quando este cliente cadastrar um atendido e contratar um plano, eles aparecem aqui." in html
    assert '<p class="cli-contato-resumo">Nenhum atendido cadastrado.</p>' in html


def test_detalhe_atendido_sem_plano(admin, db):
    cliente = criar_cliente(db)
    criar_paciente(db, cliente.id, nome="Pedro Souza")
    html = _html(admin.get(f"{URL}/{cliente.id}"))
    assert "Nenhum plano contratado até agora." in html and "<details" not in html


def test_especialidade_sem_cor_propria_usa_tom_neutro(admin, db):
    terapia = criar_funcionario(db, criar_especialidade(db, "Terapia Ocupacional", "160.00"), email="t@exemplo.com")
    cliente = criar_cliente(db)
    criar_plano(db, criar_paciente(db, cliente.id), terapia, [(datetime(2026, 3, 12, 9, 0), AGENDADA)])
    assert '<article class="cli-plano esp-neutra"' in _html(admin.get(f"{URL}/{cliente.id}"))


def test_cpf_inteiro_nunca_aparece_no_html(admin, db, juliana, carlos):
    cliente, *_ = _maria(db, juliana, carlos)
    digitos = CPF.replace(".", "").replace("-", "")
    paginas = [admin.get(f"{URL}/{cliente.id}").text, admin.get(URL).text, admin.get(URL, params={"q": "maria"}).text]
    for html in paginas:
        sem_espacos = re.sub(r"\s", "", html)
        assert CPF not in html and digitos not in sem_espacos and digitos[:9] not in sem_espacos
        assert "529.982" not in html and "982.247" not in html
    assert "***.***.***-25" in paginas[0]
    assert "***.***.***-25" not in paginas[1]  # a lista não mostra CPF nenhum


def test_detalhe_nao_mostra_dados_novos_sensiveis(admin, db, juliana, carlos):
    cliente, *_ = _maria(db, juliana, carlos)
    html = _html(admin.get(f"{URL}/{cliente.id}")).lower()
    html = html[html.index('<div class="cli-detalhe">'):html.index("</main>")]
    for proibido in ("05/01/2017", "05/01/2020", "ju@exemplo.com", "ca@exemplo.com", "avaliação", "observação", "<form"):
        assert proibido not in html


@pytest.mark.parametrize("ident", ["9999", "abc", "0", "-1", "²", "99999999999999999999"])
def test_detalhe_404_para_id_invalido_ou_inexistente(admin, ident):
    assert admin.get(f"{URL}/{ident}").status_code == 404


def test_detalhe_404_para_admin_e_profissional(admin, db, carlos):
    outro_admin = criar_admin(db, email="admin2@exemplo.com")
    assert admin.get(f"{URL}/{carlos.usuario.id}").status_code == 404
    assert admin.get(f"{URL}/{outro_admin.id}").status_code == 404


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


def test_detalhe_com_poucos_e_muitos_planos_faz_o_mesmo_numero_de_consultas(admin, db, juliana, carlos):
    cliente = criar_cliente(db)
    pedro = criar_paciente(db, cliente.id, nome="Pedro")
    criar_plano(db, pedro, carlos, [(datetime(2026, 3, 12, 9, 0), AGENDADA)])
    url = f"{URL}/{cliente.id}"
    _contar_sql(admin, db, url)  # aquece: a 1ª requisição recarrega o usuário logado
    poucos = _contar_sql(admin, db, url)
    for i in range(6):
        paciente = criar_paciente(db, cliente.id, nome=f"Atendido {i}")
        for semana in range(4):
            sessoes = [(datetime(2026, 2, 2 + semana, 9, 0), REALIZADA), (datetime(2026, 3, 16 + semana, 10, 0), AGENDADA)]
            plano = criar_plano(db, paciente, juliana if semana % 2 else carlos, sessoes)
            if semana == 3:
                _pagamento(db, plano, FormaPagamento.PIX)
    db.expire_all()
    _contar_sql(admin, db, url)
    muitos = _contar_sql(admin, db, url)
    assert muitos == poucos <= 5


def test_lista_com_poucos_e_muitos_clientes_faz_o_mesmo_numero_de_consultas(admin, db, carlos):
    criar_cliente(db)
    _contar_sql(admin, db, URL)
    poucos = _contar_sql(admin, db, URL)
    for cliente in _clientes(db, 30):
        criar_plano(db, criar_paciente(db, cliente.id), carlos, [(datetime(2026, 3, 12, 9, 0), AGENDADA)])
    db.expire_all()
    _contar_sql(admin, db, URL)
    assert _contar_sql(admin, db, URL) == poucos == 2  # contagem (a da paginação) + página
