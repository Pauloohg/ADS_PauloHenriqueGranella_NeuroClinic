import logging
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi_mail import FastMail
from sqlalchemy import event, func, select

from app.core.config import RAIZ_PROJETO, TAMANHO_MAXIMO_AVALIACAO_MB, Settings, settings
from app.main import FORMULARIOS_POR_ROTA
from app.models.avaliacao import Avaliacao
from app.models.sessao import Sessao, StatusSessao
from app.routers import agenda as agenda_router
from app.routers import inicio as inicio_router
from app.services import agenda_service, avaliacao_service
from app.services.avaliacao_service import (
    MSG_GRANDE,
    MSG_JA_REGISTRADA,
    MSG_NAO_PDF,
    MSG_PDF_INVALIDO,
    MSG_PENDENTE,
    MSG_SEM_ARQUIVO,
    MSG_VAZIO,
    AvaliacaoNaoEncontrada,
    JaRegistrada,
    RegistroBloqueado,
)
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

AGORA = datetime(2026, 10, 28, 15, 0)  # horário local, quarta-feira
AGORA_UTC = datetime(2026, 10, 28, 18, 0, tzinfo=timezone.utc)
SESSAO_ENCERRADA = datetime(2026, 10, 28, 14, 0)  # termina exatamente em AGORA
PDF = b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\ntrailer << >>\n%%EOF\n"
NOME_UUID = re.compile(r"[0-9a-f]{32}\.pdf")
PASTA_PUBLICA = Path("app/static")


@pytest.fixture(autouse=True)
def relogio_fixo(monkeypatch):
    monkeypatch.setattr(agenda_router, "agora_local", lambda: AGORA)
    monkeypatch.setattr(agenda_router, "agora_utc", lambda: AGORA_UTC)
    monkeypatch.setattr(inicio_router, "agora_local", lambda: AGORA)


@pytest.fixture
def emails(monkeypatch):
    enviados = []

    async def registrar(*args, **kwargs):
        enviados.append((args, kwargs))
        return True

    monkeypatch.setattr(avaliacao_service, "enviar_email", registrar)
    return enviados


@pytest.fixture
def fono(db):
    return criar_funcionario(db, criar_especialidade(db, "Fonoaudiologia"))


@pytest.fixture
def psico(db):
    return criar_funcionario(db, criar_especialidade(db, "Psicologia"), email="psi@exemplo.com", nome="Paula Psi")


@pytest.fixture
def cliente(db):
    return criar_cliente(db, nome="Maria Responsável")


@pytest.fixture
def paciente(db, cliente):
    return criar_paciente(db, cliente.id, nome="Pedro Souza", nascimento=date(2018, 10, 29))


@pytest.fixture
def sessao(db, paciente, fono):
    plano = criar_plano(db, paciente, fono, [(SESSAO_ENCERRADA, AGENDADA)])
    return db.scalar(select(Sessao).where(Sessao.plano_id == plano.id))


@pytest.fixture
def logado_fono(client, fono):
    logar(client, fono.usuario.email)
    return client


def _html(resposta) -> str:
    return " ".join(resposta.text.split())


def _arquivos(pasta: Path) -> list[Path]:
    return sorted(pasta.iterdir()) if pasta.exists() else []


def _enviar_pdf(client, sessao_id, conteudo=PDF, nome="avaliacao.pdf", tipo="application/pdf"):
    return client.post(
        f"/agenda/sessoes/{sessao_id}/comparecimento", files={"arquivo": (nome, conteudo, tipo)}, follow_redirects=False,
    )


def _status(db, sessao_id) -> StatusSessao:
    db.expire_all()
    return db.get(Sessao, sessao_id).status


def _avaliacoes(db) -> list[Avaliacao]:
    db.expire_all()
    return list(db.scalars(select(Avaliacao)))


# --- DECISÃO 1: quando a sessão pode ser registrada (funções puras) --------------------------------

def test_termino_e_o_inicio_mais_a_duracao_da_sessao():
    assert avaliacao_service.termino(SESSAO_ENCERRADA) == datetime(2026, 10, 28, 15, 0)


def test_exatamente_no_termino_pode_registrar():
    assert avaliacao_service.pode_registrar(AGENDADA, SESSAO_ENCERRADA, AGORA)


def test_um_minuto_antes_do_termino_nao_pode():
    motivo = avaliacao_service.motivo_bloqueio(AGENDADA, SESSAO_ENCERRADA, AGORA - timedelta(minutes=1))
    assert motivo.startswith("Só é possível registrar depois do fim da sessão")
    assert "28/10/2026 às 15:00" in motivo


def test_sessao_em_andamento_nao_pode():
    assert not avaliacao_service.pode_registrar(AGENDADA, SESSAO_ENCERRADA, SESSAO_ENCERRADA + timedelta(minutes=30))


@pytest.mark.parametrize("status, mensagem", [(PENDENTE, MSG_PENDENTE), (REALIZADA, MSG_JA_REGISTRADA)])
def test_pendente_e_realizada_ficam_bloqueadas(status, mensagem):
    passado = SESSAO_ENCERRADA - timedelta(days=7)
    assert avaliacao_service.motivo_bloqueio(status, passado, AGORA) == mensagem
    with pytest.raises(RegistroBloqueado):
        avaliacao_service.validar_registravel(status, passado, AGORA)


def test_realizada_levanta_ja_registrada():
    with pytest.raises(JaRegistrada):
        avaliacao_service.validar_registravel(REALIZADA, SESSAO_ENCERRADA, AGORA)


# --- Tela de registro ---------------------------------------------------------------------------

def test_tela_de_registro_mostra_resumo_e_dois_formularios(logado_fono, sessao):
    resposta = logado_fono.get(f"/agenda/sessoes/{sessao.id}/registrar")
    assert resposta.status_code == 200
    html = _html(resposta)
    assert "Pedro Souza" in html and "7 anos" in html and "Fonoaudiologia" in html
    assert "28/10/2026" in html and "14:00 – 15:00" in html
    assert f'action="/agenda/sessoes/{sessao.id}/comparecimento" enctype="multipart/form-data"' in html
    assert f'action="/agenda/sessoes/{sessao.id}/nao-comparecimento"' not in html  # a falta passa pela confirmação
    assert f'href="/agenda/sessoes/{sessao.id}/nao-comparecimento/confirmar"' in html
    assert "Anexar avaliação e concluir" in html and "Registrar não comparecimento" in html
    assert "não gera avaliação nem envia e-mail" in html
    assert 'type="file"' in html and "/static/js/" not in html  # sem JavaScript próprio


def test_tela_antes_do_termino_mostra_motivo_sem_formularios(monkeypatch, logado_fono, sessao):
    monkeypatch.setattr(agenda_router, "agora_local", lambda: AGORA - timedelta(minutes=1))
    html = _html(logado_fono.get(f"/agenda/sessoes/{sessao.id}/registrar"))
    assert "Só é possível registrar depois do fim da sessão" in html
    assert "Anexar avaliação e concluir" not in html


def test_enviar_antes_do_termino_nao_registra(monkeypatch, db, logado_fono, sessao, pasta_avaliacoes_temporaria):
    monkeypatch.setattr(agenda_router, "agora_local", lambda: AGORA - timedelta(minutes=1))
    resposta = _enviar_pdf(logado_fono, sessao.id)
    assert resposta.status_code == 409
    assert "Só é possível registrar depois do fim da sessão" in _html(resposta)
    resposta = logado_fono.post(f"/agenda/sessoes/{sessao.id}/nao-comparecimento", follow_redirects=False)
    assert resposta.status_code == 409
    assert _status(db, sessao.id) == AGENDADA
    assert _avaliacoes(db) == []
    assert _arquivos(pasta_avaliacoes_temporaria) == []


@pytest.mark.parametrize("status, mensagem", [(PENDENTE, MSG_PENDENTE), (REALIZADA, MSG_JA_REGISTRADA)])
def test_rotas_bloqueiam_pendente_e_realizada(db, logado_fono, paciente, fono, status, mensagem, pasta_avaliacoes_temporaria):
    plano = criar_plano(db, paciente, fono, [(datetime(2026, 10, 21, 14, 0), status)])
    sessao_id = db.scalar(select(Sessao.id).where(Sessao.plano_id == plano.id))
    assert mensagem in _html(logado_fono.get(f"/agenda/sessoes/{sessao_id}/registrar"))
    resposta = _enviar_pdf(logado_fono, sessao_id)
    assert resposta.status_code == 409 and mensagem in _html(resposta)
    resposta = logado_fono.post(f"/agenda/sessoes/{sessao_id}/nao-comparecimento", follow_redirects=False)
    assert resposta.status_code == 409 and mensagem in _html(resposta)
    assert _status(db, sessao_id) == status
    assert _avaliacoes(db) == []  # RN04
    assert _arquivos(pasta_avaliacoes_temporaria) == []


def test_sessao_de_outro_profissional_e_inexistente_dao_404(db, client, psico, sessao):
    logar(client, psico.usuario.email)
    for sessao_id in (sessao.id, 9999):
        assert client.get(f"/agenda/sessoes/{sessao_id}/registrar").status_code == 404
        assert _enviar_pdf(client, sessao_id).status_code == 404
        assert client.post(f"/agenda/sessoes/{sessao_id}/nao-comparecimento").status_code == 404
    assert _status(db, sessao.id) == AGENDADA


@pytest.mark.parametrize("perfil", ["cliente", "admin"])
def test_cliente_e_admin_nao_acessam_as_rotas_de_registro(db, client, cliente, sessao, perfil):
    email = cliente.email if perfil == "cliente" else criar_admin(db).email
    logar(client, email)
    assert client.get(f"/agenda/sessoes/{sessao.id}/registrar").status_code == 403
    assert _enviar_pdf(client, sessao.id).status_code == 403
    assert client.post(f"/agenda/sessoes/{sessao.id}/nao-comparecimento").status_code == 403
    assert _status(db, sessao.id) == AGENDADA


def test_visitante_vai_para_o_login_nas_rotas_de_registro(client, sessao):
    resposta = client.get(f"/agenda/sessoes/{sessao.id}/registrar", follow_redirects=False)
    assert resposta.status_code == 303 and resposta.headers["location"] == "/login"


def test_rotas_post_estao_em_formularios_por_rota():
    assert FORMULARIOS_POR_ROTA["/agenda/sessoes/{sessao_id}/comparecimento"] == "funcionario/registrar_atendimento.html"
    assert FORMULARIOS_POR_ROTA["/agenda/sessoes/{sessao_id}/nao-comparecimento"] == "funcionario/registrar_atendimento.html"


# --- Registro com comparecimento (HU11) ---------------------------------------------------------

def test_registro_com_pdf_valido(db, logado_fono, sessao, emails, pasta_avaliacoes_temporaria):
    resposta = _enviar_pdf(logado_fono, sessao.id)
    assert resposta.status_code == 303
    assert resposta.headers["location"] == "/agenda?visao=dia&data=2026-10-28&registrado=comparecimento"
    assert _status(db, sessao.id) == REALIZADA
    [avaliacao] = _avaliacoes(db)
    assert avaliacao.sessao_id == sessao.id and avaliacao.comparecimento is True
    assert NOME_UUID.fullmatch(avaliacao.arquivo_url)
    assert avaliacao.liberado_em == AGORA_UTC.replace(tzinfo=None)  # UTC sem fuso, como criado_em e confirmado_em
    [arquivo] = _arquivos(pasta_avaliacoes_temporaria)
    assert arquivo.name == avaliacao.arquivo_url and arquivo.read_bytes() == PDF
    assert len(emails) == 1


def test_mensagem_de_sucesso_na_agenda(logado_fono, sessao, emails):
    html = _html(_enviar_pdf(logado_fono, sessao.id) and logado_fono.get(
        "/agenda?visao=dia&data=2026-10-28&registrado=comparecimento"
    ))
    assert "Atendimento registrado." in html


def test_pdf_no_limite_exato_e_aceito(db, logado_fono, sessao, emails):
    conteudo = PDF + b"0" * (TAMANHO_MAXIMO_AVALIACAO_MB * 1024 * 1024 - len(PDF))
    assert _enviar_pdf(logado_fono, sessao.id, conteudo).status_code == 303
    assert _status(db, sessao.id) == REALIZADA


@pytest.mark.parametrize("nome, conteudo, mensagem", [
    ("avaliacao.pdf", b"MZ\x90\x00 nao sou pdf", MSG_PDF_INVALIDO),
    ("avaliacao.pdf", b"<html><script>alert(1)</script>", MSG_PDF_INVALIDO),
    ("avaliacao.pdf", b"", MSG_VAZIO),
    ("avaliacao.docx", PDF, MSG_NAO_PDF),
    ("avaliacao.pdf.exe", PDF, MSG_NAO_PDF),
    ("avaliacao", PDF, MSG_NAO_PDF),
    ("", PDF, MSG_SEM_ARQUIVO),
])
def test_arquivo_invalido_reabre_a_tela_com_mensagem(
    db, logado_fono, sessao, emails, pasta_avaliacoes_temporaria, nome, conteudo, mensagem,
):
    resposta = _enviar_pdf(logado_fono, sessao.id, conteudo, nome)
    assert resposta.status_code == 400
    html = _html(resposta)
    assert mensagem in html and "Anexar avaliação e concluir" in html
    assert _status(db, sessao.id) == AGENDADA
    assert _avaliacoes(db) == [] and emails == []
    assert _arquivos(pasta_avaliacoes_temporaria) == []


def test_mensagem_do_arquivo_aparece_no_proprio_campo(logado_fono, sessao, emails):
    html = _html(_enviar_pdf(logado_fono, sessao.id, b"texto qualquer"))
    assert f'<div class="invalid-feedback" role="alert">{MSG_PDF_INVALIDO}</div>' in html
    assert 'class="form-control is-invalid"' in html


def test_formulario_sem_arquivo_reabre_a_tela(db, logado_fono, sessao, emails):
    resposta = logado_fono.post(f"/agenda/sessoes/{sessao.id}/comparecimento", follow_redirects=False)
    assert resposta.status_code == 400
    assert MSG_SEM_ARQUIVO in _html(resposta)
    assert _status(db, sessao.id) == AGENDADA


def test_arquivo_acima_do_limite_e_recusado(db, logado_fono, sessao, emails, pasta_avaliacoes_temporaria):
    conteudo = PDF + b"0" * (TAMANHO_MAXIMO_AVALIACAO_MB * 1024 * 1024 + 1 - len(PDF))
    resposta = _enviar_pdf(logado_fono, sessao.id, conteudo)
    assert resposta.status_code == 400
    assert MSG_GRANDE in _html(resposta)
    assert _status(db, sessao.id) == AGENDADA
    assert _arquivos(pasta_avaliacoes_temporaria) == []  # nem o arquivo parcial fica


def test_tamanho_e_lido_em_blocos_e_para_no_limite(monkeypatch, tmp_path):
    lidos = []

    class Fonte:
        def read(self, tamanho):
            lidos.append(tamanho)
            return (PDF + b"0" * (tamanho - len(PDF))) if len(lidos) == 1 else b"0" * tamanho

    monkeypatch.setattr(avaliacao_service, "TAMANHO_MAXIMO_BYTES", 3 * avaliacao_service.TAMANHO_BLOCO)
    with pytest.raises(avaliacao_service.ArquivoInvalido):
        avaliacao_service.salvar_arquivo(Fonte(), "a.pdf")
    assert set(lidos) == {avaliacao_service.TAMANHO_BLOCO} and len(lidos) == 4


@pytest.mark.parametrize("nome", [
    "../../../evil.pdf", "..\\..\\evil.pdf", "/etc/passwd.pdf", "avaliação <script>alert(1)</script>.pdf",
    "C:\\Windows\\evil.pdf", "nome%00.pdf",
])
def test_nome_malicioso_nunca_e_usado(db, logado_fono, sessao, emails, pasta_avaliacoes_temporaria, tmp_path, nome):
    assert _enviar_pdf(logado_fono, sessao.id, nome=nome).status_code == 303
    [avaliacao] = _avaliacoes(db)
    assert NOME_UUID.fullmatch(avaliacao.arquivo_url)
    assert [a.name for a in _arquivos(pasta_avaliacoes_temporaria)] == [avaliacao.arquivo_url]
    assert not list(tmp_path.rglob("evil.pdf")) and not (tmp_path / "passwd.pdf").exists()


def test_arquivo_fica_fora_da_pasta_publica(db, logado_fono, sessao, emails, pasta_avaliacoes_temporaria):
    antes = set(PASTA_PUBLICA.rglob("*"))
    _enviar_pdf(logado_fono, sessao.id)
    nome = _avaliacoes(db)[0].arquivo_url
    assert set(PASTA_PUBLICA.rglob("*")) == antes
    assert not list(PASTA_PUBLICA.rglob("*.pdf"))
    assert logado_fono.get(f"/static/{nome}").status_code == 404


def test_pasta_padrao_fica_fora_de_app_static_e_no_gitignore():
    padrao = Path(Settings.model_fields["AVALIACOES_DIR"].default).resolve()
    assert not padrao.is_relative_to(PASTA_PUBLICA.resolve())
    assert not padrao.is_relative_to(Path("app").resolve())
    linhas = Path(".gitignore").read_text(encoding="utf-8").splitlines()
    assert Path(Settings.model_fields["AVALIACOES_DIR"].default).parts[0] + "/" in linhas


def test_duplo_envio_nao_duplica_avaliacao_nem_arquivo(db, logado_fono, sessao, emails, pasta_avaliacoes_temporaria):
    assert _enviar_pdf(logado_fono, sessao.id).status_code == 303
    resposta = _enviar_pdf(logado_fono, sessao.id)
    assert resposta.status_code == 409
    assert MSG_JA_REGISTRADA in _html(resposta)
    resposta = logado_fono.post(f"/agenda/sessoes/{sessao.id}/nao-comparecimento", follow_redirects=False)
    assert resposta.status_code == 409
    assert len(_avaliacoes(db)) == 1 and _avaliacoes(db)[0].comparecimento is True
    assert len(_arquivos(pasta_avaliacoes_temporaria)) == 1
    assert len(emails) == 1


def test_outra_aba_registrando_ao_mesmo_tempo_nao_deixa_arquivo_orfao(
    monkeypatch, db, fono, sessao, pasta_avaliacoes_temporaria,
):
    avaliacao_service.registrar_comparecimento(
        db, fono.usuario.id, sessao.id, _Fonte(PDF), "a.pdf", AGORA, AGORA_UTC.replace(tzinfo=None), "http://x/",
    )
    # A segunda aba leu a sessão ainda "agendada" antes de a primeira concluir: só o UPDATE condicional a barra
    monkeypatch.setattr(avaliacao_service, "validar_registravel", lambda *args: None)
    with pytest.raises(JaRegistrada):
        avaliacao_service.registrar_comparecimento(
            db, fono.usuario.id, sessao.id, _Fonte(PDF), "b.pdf", AGORA, AGORA_UTC.replace(tzinfo=None), "http://x/",
        )
    with pytest.raises(JaRegistrada):
        avaliacao_service.registrar_nao_comparecimento(db, fono.usuario.id, sessao.id, AGORA, AGORA_UTC.replace(tzinfo=None))
    assert len(_avaliacoes(db)) == 1
    assert len(_arquivos(pasta_avaliacoes_temporaria)) == 1


def test_falha_na_transacao_apaga_o_arquivo(monkeypatch, db, fono, sessao, pasta_avaliacoes_temporaria):
    def falhar():
        raise RuntimeError("banco caiu")

    monkeypatch.setattr(db, "commit", falhar)
    with pytest.raises(RuntimeError):
        avaliacao_service.registrar_comparecimento(
            db, fono.usuario.id, sessao.id, _Fonte(PDF), "a.pdf", AGORA, AGORA_UTC.replace(tzinfo=None), "http://x/",
        )
    monkeypatch.undo()
    assert _arquivos(pasta_avaliacoes_temporaria) == []
    assert _status(db, sessao.id) == AGENDADA
    assert _avaliacoes(db) == []


def test_falha_ao_gravar_o_arquivo_nao_muda_o_banco(monkeypatch, db, logado_fono, sessao, emails):
    def falhar(*args):
        raise OSError("disco cheio")

    monkeypatch.setattr(avaliacao_service, "salvar_arquivo", falhar)
    resposta = _enviar_pdf(logado_fono, sessao.id)
    assert resposta.status_code == 503
    assert agenda_router.MSG_FALHA_GRAVACAO in _html(resposta)
    assert _status(db, sessao.id) == AGENDADA
    assert _avaliacoes(db) == [] and emails == []


class _Fonte:
    def __init__(self, conteudo: bytes):
        self._conteudo = conteudo
        self._posicao = 0

    def read(self, tamanho: int) -> bytes:
        bloco = self._conteudo[self._posicao:self._posicao + tamanho]
        self._posicao += len(bloco)
        return bloco


# --- Não comparecimento (HU12) ------------------------------------------------------------------

def test_nao_comparecimento_sem_arquivo_e_sem_email(db, logado_fono, sessao, emails, pasta_avaliacoes_temporaria):
    resposta = logado_fono.post(f"/agenda/sessoes/{sessao.id}/nao-comparecimento", follow_redirects=False)
    assert resposta.status_code == 303
    assert resposta.headers["location"] == "/agenda?visao=dia&data=2026-10-28&registrado=falta"
    assert _status(db, sessao.id) == REALIZADA
    [avaliacao] = _avaliacoes(db)
    assert avaliacao.comparecimento is False and avaliacao.arquivo_url is None
    assert avaliacao.liberado_em == AGORA_UTC.replace(tzinfo=None)
    assert emails == []
    assert _arquivos(pasta_avaliacoes_temporaria) == []


def test_nao_comparecimento_duplo_envio(db, logado_fono, sessao, emails):
    logado_fono.post(f"/agenda/sessoes/{sessao.id}/nao-comparecimento")
    resposta = logado_fono.post(f"/agenda/sessoes/{sessao.id}/nao-comparecimento", follow_redirects=False)
    assert resposta.status_code == 409 and MSG_JA_REGISTRADA in _html(resposta)
    resposta = _enviar_pdf(logado_fono, sessao.id)
    assert resposta.status_code == 409
    assert len(_avaliacoes(db)) == 1


# --- E-mail ao Cliente (RNF09) ------------------------------------------------------------------

def test_email_avisa_sem_conteudo_clinico_e_sem_anexo(db, logado_fono, sessao, cliente, paciente, emails):
    plano = db.get(Sessao, sessao.id).plano_id
    from app.models.plano import Plano
    db.get(Plano, plano).observacao = "Dificuldade de leitura e escrita"
    db.commit()
    _enviar_pdf(logado_fono, sessao.id)
    [(args, kwargs)] = emails
    destinatario, assunto, corpo = args
    assert kwargs == {} and len(args) == 3  # enviar_email não tem parâmetro de anexo
    assert destinatario == cliente.email
    assert assunto == "Nova avaliação disponível"
    texto = " ".join(corpo.split())
    assert "Há uma nova avaliação disponível para <strong>Pedro Souza</strong>, referente à sessão do dia 28/10/2026." in texto
    assert f'href="http://testserver/historico?atendido={paciente.id}"' in texto
    nome_arquivo = _avaliacoes(db)[0].arquivo_url
    for proibido in ("Dificuldade", "Fonoaudiologia", "Carlos Fono", nome_arquivo, "/avaliacoes/", "%PDF"):
        assert proibido not in corpo


def test_email_so_e_montado_depois_do_commit(monkeypatch, db, fono, sessao):
    momentos = []
    original = avaliacao_service.montar_email_nova_avaliacao

    def espiar(db_, sessao_id, url_base):
        momentos.append(db_.get(Sessao, sessao_id).status)
        return original(db_, sessao_id, url_base)

    class Tarefas:
        def __init__(self):
            self.tarefas = []

        def add_task(self, *args):
            self.tarefas.append(args)

    monkeypatch.setattr(avaliacao_service, "montar_email_nova_avaliacao", espiar)
    tarefas = Tarefas()
    avaliacao_service.registrar_comparecimento(
        db, fono.usuario.id, sessao.id, _Fonte(PDF), "a.pdf", AGORA, AGORA_UTC.replace(tzinfo=None), "http://x/", tarefas,
    )
    db.rollback()  # o que foi gravado sobrevive: o commit já aconteceu
    assert _status(db, sessao.id) == REALIZADA
    assert momentos == [REALIZADA] and len(tarefas.tarefas) == 1


def test_falha_de_smtp_nao_quebra_o_registro(monkeypatch, db, logado_fono, sessao, caplog):
    async def explodir(self, mensagem):
        raise ConnectionError("SMTP fora do ar")

    monkeypatch.setattr(FastMail, "send_message", explodir)
    with caplog.at_level(logging.ERROR):
        resposta = _enviar_pdf(logado_fono, sessao.id)
    assert resposta.status_code == 303
    assert _status(db, sessao.id) == REALIZADA
    assert "Falha ao enviar e-mail" in caplog.text


# --- Download seguro (RN03 e RNF08) -------------------------------------------------------------

@pytest.fixture
def avaliacao(db, fono, sessao):
    avaliacao_service.registrar_comparecimento(
        db, fono.usuario.id, sessao.id, _Fonte(PDF), "a.pdf", AGORA, AGORA_UTC.replace(tzinfo=None), "http://x/",
    )
    return _avaliacoes(db)[0]


def _baixar(client, avaliacao_id):
    return client.get(f"/avaliacoes/{avaliacao_id}/arquivo", follow_redirects=False)


def test_cliente_dono_baixa(client, cliente, avaliacao):
    logar(client, cliente.email)
    resposta = _baixar(client, avaliacao.id)
    assert resposta.status_code == 200 and resposta.content == PDF


def test_outro_cliente_recebe_404(db, client, avaliacao):
    outro = criar_cliente(db, email="outro@exemplo.com", nome="Outra Pessoa")
    criar_paciente(db, outro.id, nome="Outro Atendido")
    logar(client, outro.email)
    assert _baixar(client, avaliacao.id).status_code == 404


def test_admin_baixa(db, client, avaliacao):
    logar(client, criar_admin(db).email)
    assert _baixar(client, avaliacao.id).status_code == 200


def test_profissional_que_atendeu_baixa(logado_fono, avaliacao):
    assert _baixar(logado_fono, avaliacao.id).status_code == 200


@pytest.mark.parametrize("status", [AGENDADA, REALIZADA])
def test_outro_profissional_com_consulta_do_atendido_baixa(db, client, psico, paciente, avaliacao, status):
    criar_plano(db, paciente, psico, [(datetime(2026, 11, 3, 9, 0), status)])
    logar(client, psico.usuario.email)
    assert _baixar(client, avaliacao.id).status_code == 200


def test_profissional_sem_vinculo_recebe_404(db, client, psico, cliente, avaliacao):
    criar_plano(db, criar_paciente(db, cliente.id, nome="Irmã"), psico, [(datetime(2026, 11, 3, 9, 0), AGENDADA)])
    logar(client, psico.usuario.email)
    assert _baixar(client, avaliacao.id).status_code == 404


def test_plano_so_aguardando_pagamento_nao_cria_vinculo(db, client, psico, paciente, avaliacao):
    criar_plano(db, paciente, psico, [(datetime(2026, 11, 3, 9, 0), PENDENTE)])
    logar(client, psico.usuario.email)
    assert _baixar(client, avaliacao.id).status_code == 404


def test_visitante_vai_para_o_login(client, avaliacao):
    resposta = _baixar(client, avaliacao.id)
    assert resposta.status_code == 303 and resposta.headers["location"] == "/login"


def test_inexistente_e_nao_comparecimento_dao_404(db, client, cliente, logado_fono, paciente, fono):
    plano = criar_plano(db, paciente, fono, [(datetime(2026, 10, 21, 14, 0), AGENDADA)])
    sessao_id = db.scalar(select(Sessao.id).where(Sessao.plano_id == plano.id))
    avaliacao_service.registrar_nao_comparecimento(db, fono.usuario.id, sessao_id, AGORA, AGORA_UTC.replace(tzinfo=None))
    falta = _avaliacoes(db)[0]
    logar(client, cliente.email)
    assert _baixar(client, falta.id).status_code == 404
    assert _baixar(client, 9999).status_code == 404


def test_cabecalhos_do_download(client, cliente, avaliacao):
    logar(client, cliente.email)
    resposta = _baixar(client, avaliacao.id)
    assert resposta.headers["content-type"] == "application/pdf"
    assert resposta.headers["content-disposition"] == 'inline; filename="avaliacao-2026-10-28.pdf"'
    assert resposta.headers["cache-control"] == "private, no-store"
    assert resposta.headers["x-content-type-options"] == "nosniff"
    assert avaliacao.arquivo_url not in resposta.headers["content-disposition"]


def test_arquivo_ausente_no_disco_da_404_e_loga_so_o_id(client, cliente, avaliacao, pasta_avaliacoes_temporaria, caplog):
    (pasta_avaliacoes_temporaria / avaliacao.arquivo_url).unlink()
    logar(client, cliente.email)
    with caplog.at_level(logging.WARNING):
        assert _baixar(client, avaliacao.id).status_code == 404
    assert f"Arquivo da avaliação {avaliacao.id} não encontrado no disco" in caplog.text
    for proibido in (avaliacao.arquivo_url, str(pasta_avaliacoes_temporaria), "Pedro Souza"):
        assert proibido not in caplog.text


def test_nome_adulterado_no_banco_nao_escapa_da_pasta(db, client, cliente, avaliacao, tmp_path):
    (tmp_path / "segredo.pdf").write_bytes(PDF)
    avaliacao.arquivo_url = "../segredo.pdf"
    db.commit()
    logar(client, cliente.email)
    assert _baixar(client, avaliacao.id).status_code == 404


@pytest.mark.parametrize("nome", [
    "../x.pdf", "..\\x.pdf", "/etc/passwd", "a" * 32 + ".pdf/../../x", "", "A" * 32 + ".pdf", "a" * 32 + ".PDF",
])
def test_caminho_do_arquivo_recusa_nomes_fora_do_padrao(nome):
    with pytest.raises(AvaliacaoNaoEncontrada):
        avaliacao_service.caminho_do_arquivo(nome)


def test_caminho_do_arquivo_fica_dentro_da_pasta(pasta_avaliacoes_temporaria):
    caminho = avaliacao_service.caminho_do_arquivo("0" * 32 + ".pdf")
    assert caminho.parent == pasta_avaliacoes_temporaria.resolve()


# --- Agenda: botão, aba "A registrar" e selos ---------------------------------------------------

def test_agenda_mostra_botao_so_nas_sessoes_elegiveis(db, logado_fono, paciente, fono):
    criar_plano(db, paciente, fono, [
        (datetime(2026, 10, 28, 14, 0), AGENDADA),  # terminou agora: elegível
        (datetime(2026, 10, 28, 15, 0), AGENDADA),  # em andamento
        (datetime(2026, 10, 29, 9, 0), AGENDADA),  # futura
    ])
    sessoes = list(db.scalars(select(Sessao.id).order_by(Sessao.data_hora)))
    html = _html(logado_fono.get("/agenda"))
    assert html.count("Registrar atendimento<span") == 1
    assert f'href="/agenda/sessoes/{sessoes[0]}/registrar"' in html
    assert f'href="/agenda/sessoes/{sessoes[1]}/registrar"' not in html


def test_aba_a_registrar_lista_so_as_elegiveis_da_mais_antiga_para_a_mais_recente(db, logado_fono, paciente, fono, psico):
    criar_plano(db, paciente, fono, [
        (datetime(2026, 10, 27, 9, 0), AGENDADA),
        (datetime(2026, 10, 14, 9, 0), AGENDADA),
        (datetime(2026, 10, 28, 14, 0), AGENDADA),
        (datetime(2026, 10, 21, 9, 0), REALIZADA),
        (datetime(2026, 10, 28, 15, 0), AGENDADA),  # ainda não terminou
        (datetime(2026, 10, 30, 9, 0), AGENDADA),
    ])
    criar_plano(db, paciente, fono, [(datetime(2026, 10, 20, 9, 0), PENDENTE)])
    criar_plano(db, paciente, psico, [(datetime(2026, 10, 19, 9, 0), AGENDADA)])
    resposta = logado_fono.get("/agenda", params={"visao": "registrar"})
    assert resposta.status_code == 200
    html = _html(resposta)
    datas = re.findall(r'agenda-consulta-data">(\d\d/\d\d/\d{4})</span> (\d\d:\d\d)', html)
    assert datas == [("14/10/2026", "09:00"), ("27/10/2026", "09:00"), ("28/10/2026", "14:00")]
    assert 'aria-current="page">A registrar' in html
    assert "3 consultas" in html


def test_aba_a_registrar_pagina_de_20_em_20(db, logado_fono, paciente, fono):
    inicio = datetime(2026, 9, 1, 9, 0)
    criar_plano(db, paciente, fono, [(inicio + timedelta(days=i), AGENDADA) for i in range(25)])
    pagina1 = _html(logado_fono.get("/agenda", params={"visao": "registrar"}))
    pagina2 = _html(logado_fono.get("/agenda", params={"visao": "registrar", "pagina": "2"}))
    assert pagina1.count("Registrar atendimento<span") == 20
    assert pagina2.count("Registrar atendimento<span") == 5
    assert "Página 1 de 2" in pagina1 and "01/09/2026" in pagina1 and "21/09/2026" not in pagina1
    assert "21/09/2026" in pagina2 and "25/09/2026" in pagina2
    resultado = avaliacao_service.consultas_a_registrar(db, fono.usuario.id, AGORA, 99)
    assert resultado.pagina == 2 and len(resultado.consultas) == 5


def test_aba_a_registrar_vazia(logado_fono):
    html = _html(logado_fono.get("/agenda", params={"visao": "registrar"}))
    assert "Nenhuma consulta aguardando registro." in html


def test_aba_a_registrar_nao_altera_as_outras_visoes(db, logado_fono, paciente, fono):
    criar_plano(db, paciente, fono, [(datetime(2026, 10, 14, 9, 0), AGENDADA)])
    html = _html(logado_fono.get("/agenda"))
    assert "26/10/2026 a 30/10/2026" in html and "14/10/2026" not in html
    assert '<span class="badge rounded-pill agenda-contador-registrar">1</span>' in html


def test_agenda_mostra_selos_de_avaliacao_e_falta(db, logado_fono, paciente, fono, emails):
    plano = criar_plano(db, paciente, fono, [
        (datetime(2026, 10, 27, 9, 0), AGENDADA), (datetime(2026, 10, 28, 9, 0), AGENDADA),
    ])
    primeira, segunda = db.scalars(select(Sessao.id).where(Sessao.plano_id == plano.id).order_by(Sessao.data_hora))
    _enviar_pdf(logado_fono, primeira)
    logado_fono.post(f"/agenda/sessoes/{segunda}/nao-comparecimento")
    avaliacao_id = db.scalar(select(Avaliacao.id).where(Avaliacao.sessao_id == primeira))
    html = _html(logado_fono.get("/agenda"))
    assert "Avaliação enviada" in html and "Não compareceu" in html
    assert f'href="/avaliacoes/{avaliacao_id}/arquivo"' in html and "Ver avaliação" in html
    assert html.count("Ver avaliação<span") == 1
    assert "Registrar atendimento<span" not in html


def test_agenda_com_avaliacoes_continua_com_uma_consulta_sql(db, fono, paciente):
    plano = criar_plano(db, paciente, fono, [(datetime(2026, 10, 26 + i, 9, 0), AGENDADA) for i in range(3)])
    for sessao_id in db.scalars(select(Sessao.id).where(Sessao.plano_id == plano.id)).all():
        avaliacao_service.registrar_nao_comparecimento(db, fono.usuario.id, sessao_id, AGORA, AGORA_UTC.replace(tzinfo=None))
    funcionario_id = fono.usuario.id
    db.expire_all()
    comandos = []
    ouvinte = lambda *args: comandos.append(args[2])  # noqa: E731
    event.listen(db.get_bind(), "before_cursor_execute", ouvinte)
    try:
        consultas = agenda_service.consultas_do_profissional(
            db, funcionario_id, date(2026, 10, 26), date(2026, 10, 30), AGORA.date(), AGORA,
        )
    finally:
        event.remove(db.get_bind(), "before_cursor_execute", ouvinte)
    assert len(consultas) == 3 and all(c.comparecimento is False for c in consultas)
    assert len(comandos) == 1
    assert "LEFT OUTER JOIN avaliacao" in comandos[0]


# --- Tela inicial do Profissional ---------------------------------------------------------------

def test_tela_inicial_mostra_aguardando_registro(db, logado_fono, paciente, fono):
    criar_plano(db, paciente, fono, [
        (datetime(2026, 10, 27, 9, 0), AGENDADA), (datetime(2026, 10, 28, 9, 0), AGENDADA),
        (datetime(2026, 10, 28, 15, 0), AGENDADA),
    ])
    html = _html(logado_fono.get("/"))
    assert 'Aguardando registro: <span class="font-monospace-num">2</span> consultas' in html
    assert 'href="/agenda?visao=registrar"' in html


def test_tela_inicial_sem_pendencias_nao_mostra_o_bloco(db, logado_fono, paciente, fono):
    criar_plano(db, paciente, fono, [(datetime(2026, 10, 28, 15, 0), AGENDADA)])
    html = _html(logado_fono.get("/"))
    assert "Aguardando registro" not in html


def test_contar_a_registrar_ignora_outro_profissional(db, paciente, fono, psico):
    criar_plano(db, paciente, psico, [(datetime(2026, 10, 27, 9, 0), AGENDADA)])
    assert avaliacao_service.contar_a_registrar(db, fono.usuario.id, AGORA) == 0
    assert avaliacao_service.contar_a_registrar(db, psico.usuario.id, AGORA) == 1


def test_tabela_avaliacao_conta_uma_por_sessao(db, fono, sessao):
    avaliacao_service.registrar_nao_comparecimento(db, fono.usuario.id, sessao.id, AGORA, AGORA_UTC.replace(tzinfo=None))
    assert db.scalar(select(func.count()).select_from(Avaliacao)) == 1


# --- Verificações da Semana 7 (pasta a partir da raiz e PDF em nova aba) ----------------------------

def test_pasta_relativa_e_resolvida_a_partir_da_raiz_do_projeto(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # servidor iniciado de outro diretório
    monkeypatch.setattr(settings, "AVALIACOES_DIR", "dados/avaliacoes")
    assert (RAIZ_PROJETO / "app" / "main.py").is_file()
    assert avaliacao_service.pasta_avaliacoes() == (RAIZ_PROJETO / "dados" / "avaliacoes").resolve()
    assert not avaliacao_service.pasta_avaliacoes().is_relative_to(tmp_path)


def test_pasta_absoluta_do_env_e_usada_como_esta(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "AVALIACOES_DIR", str(tmp_path / "outra"))
    assert avaliacao_service.pasta_avaliacoes() == (tmp_path / "outra").resolve()


def test_ver_avaliacao_na_agenda_abre_em_nova_aba(db, logado_fono, sessao, emails):
    _enviar_pdf(logado_fono, sessao.id)
    avaliacao_id = _avaliacoes(db)[0].id
    html = _html(logado_fono.get("/agenda?visao=dia&data=2026-10-28"))
    assert f'href="/avaliacoes/{avaliacao_id}/arquivo" target="_blank" rel="noopener"' in html
    assert "(abre em nova aba)" in html


# --- Confirmação antes do registro definitivo ------------------------------------------------------

def _confirmar_url(sessao_id) -> str:
    return f"/agenda/sessoes/{sessao_id}/nao-comparecimento/confirmar"


def test_tela_de_confirmacao_mostra_atendido_data_e_aviso(logado_fono, sessao):
    resposta = logado_fono.get(_confirmar_url(sessao.id))
    assert resposta.status_code == 200
    html = _html(resposta)
    assert "Pedro Souza" in html and "7 anos" in html and "Fonoaudiologia" in html
    assert "28/10/2026" in html and "14:00" in html and "15:00" in html
    assert "A consulta passa a constar como realizada, sem avaliação." in html
    assert "O responsável não recebe e-mail." in html
    assert "O registro não pode ser desfeito." in html


def test_tela_de_confirmacao_tem_o_formulario_e_o_voltar(logado_fono, sessao):
    html = _html(logado_fono.get(_confirmar_url(sessao.id)))
    form = re.search(r'<form method="post" action="([^"]+)">(.*?)</form>', html)
    assert form and form.group(1) == f"/agenda/sessoes/{sessao.id}/nao-comparecimento"
    assert "Confirmar não comparecimento" in form.group(2)
    assert f'href="/agenda/sessoes/{sessao.id}/registrar" class="btn btn-outline-primary">Voltar</a>' in html


def test_abrir_a_confirmacao_nao_muda_nada(db, logado_fono, sessao, emails):
    for _ in range(2):
        assert logado_fono.get(_confirmar_url(sessao.id)).status_code == 200
    assert _status(db, sessao.id) == AGENDADA
    assert _avaliacoes(db) == [] and emails == []


def test_confirmacao_de_outro_profissional_e_inexistente_da_404(db, client, psico, sessao):
    logar(client, psico.usuario.email)
    for sessao_id in (sessao.id, 9999):
        assert client.get(_confirmar_url(sessao_id)).status_code == 404
    assert _status(db, sessao.id) == AGENDADA


def test_confirmacao_antes_do_termino_mostra_o_motivo_sem_formulario(monkeypatch, logado_fono, sessao):
    monkeypatch.setattr(agenda_router, "agora_local", lambda: AGORA - timedelta(minutes=1))
    html = _html(logado_fono.get(_confirmar_url(sessao.id)))
    assert "Só é possível registrar depois do fim da sessão (28/10/2026 às 15:00)." in html
    assert "Confirmar não comparecimento</button>" not in html and "/nao-comparecimento\"" not in html


@pytest.mark.parametrize("status, mensagem", [(PENDENTE, MSG_PENDENTE), (REALIZADA, MSG_JA_REGISTRADA)])
def test_confirmacao_de_pendente_e_realizada_mostra_a_mesma_mensagem(db, logado_fono, paciente, fono, status, mensagem):
    plano = criar_plano(db, paciente, fono, [(datetime(2026, 10, 21, 14, 0), status)])
    sessao_id = db.scalar(select(Sessao.id).where(Sessao.plano_id == plano.id))
    html = _html(logado_fono.get(_confirmar_url(sessao_id)))
    assert mensagem in html and mensagem in _html(logado_fono.get(f"/agenda/sessoes/{sessao_id}/registrar"))
    assert "Confirmar não comparecimento</button>" not in html


def test_confirmacao_depois_de_registrar_mostra_ja_registrada(logado_fono, sessao):
    logado_fono.post(f"/agenda/sessoes/{sessao.id}/nao-comparecimento")
    html = _html(logado_fono.get(_confirmar_url(sessao.id)))
    assert MSG_JA_REGISTRADA in html and "Confirmar não comparecimento</button>" not in html


@pytest.mark.parametrize("perfil", ["cliente", "admin"])
def test_cliente_e_admin_nao_acessam_a_confirmacao(db, client, cliente, sessao, perfil):
    logar(client, cliente.email if perfil == "cliente" else criar_admin(db).email)
    assert client.get(_confirmar_url(sessao.id)).status_code == 403


def test_visitante_vai_para_o_login_na_confirmacao(client, sessao):
    resposta = client.get(_confirmar_url(sessao.id), follow_redirects=False)
    assert resposta.status_code == 303 and resposta.headers["location"] == "/login"


def test_fluxo_completo_registrar_confirmar_e_selo_na_agenda(db, logado_fono, sessao, emails):
    registrar = _html(logado_fono.get(f"/agenda/sessoes/{sessao.id}/registrar"))
    assert _confirmar_url(sessao.id) in registrar
    assert f"/agenda/sessoes/{sessao.id}/nao-comparecimento" in _html(logado_fono.get(_confirmar_url(sessao.id)))
    resposta = logado_fono.post(f"/agenda/sessoes/{sessao.id}/nao-comparecimento", follow_redirects=False)
    assert resposta.status_code == 303
    assert resposta.headers["location"] == "/agenda?visao=dia&data=2026-10-28&registrado=falta"
    assert "Não compareceu" in _html(logado_fono.get(resposta.headers["location"]))
    assert emails == [] and _avaliacoes(db)[0].comparecimento is False


def test_tela_de_registro_tem_checkbox_obrigatorio_com_atendido_e_data(logado_fono, sessao):
    html = _html(logado_fono.get(f"/agenda/sessoes/{sessao.id}/registrar"))
    form = re.search(r'<form method="post" action="/agenda/sessoes/\d+/comparecimento".*?</form>', html).group(0)
    assert re.search(r'<input class="form-check-input" type="checkbox" id="confirmo-arquivo" name="confirmo_arquivo" value="1" required>', form)
    assert "Confirmo que este arquivo é a avaliação de <strong>Pedro Souza</strong> referente à consulta de" in form
    assert '<span class="font-monospace-num">28/10/2026</span>. Depois de enviado, não será possível trocá-lo.' in form


def test_servidor_nao_exige_o_checkbox(db, logado_fono, sessao, emails):
    assert _enviar_pdf(logado_fono, sessao.id).status_code == 303  # envio sem o campo confirmo_arquivo
    assert _status(db, sessao.id) == REALIZADA


def test_confirmacoes_nao_usam_javascript(logado_fono, sessao):
    pasta_js = Path("app/static/js")
    assert sorted(a.name for a in pasta_js.iterdir()) == ["contratar.js", "remarcar.js"]
    for url in (f"/agenda/sessoes/{sessao.id}/registrar", _confirmar_url(sessao.id)):
        html = logado_fono.get(url).text
        scripts = re.findall(r"<script[^>]*>", html)
        assert scripts == ['<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js">']
        assert "onclick" not in html and "onsubmit" not in html
