import asyncio
import logging
from datetime import datetime, timezone

from fastapi_mail import FastMail

from app.core import email
from app.schemas.usuario import ClienteCadastro
from app.services import auth_service

FORM_CADASTRO = {"nome": "João Lima", "email": "joao@exemplo.com", "telefone": "54 98888-0000", "senha": "senha123"}


def _cadastrar(db):
    auth_service.cadastrar_cliente(db, ClienteCadastro(**FORM_CADASTRO), datetime.now(timezone.utc))


def _registrar_chamadas(monkeypatch) -> list[tuple]:
    chamadas = []

    async def falso_enviar_email(destinatario, assunto, corpo_html):
        chamadas.append((destinatario, assunto, corpo_html))
        return True

    monkeypatch.setattr(auth_service, "enviar_email", falso_enviar_email)
    return chamadas


def test_solicitar_redefinicao_envia_email_com_link(client, db, monkeypatch):
    _cadastrar(db)
    chamadas = _registrar_chamadas(monkeypatch)

    resposta = client.post("/esqueci-senha", data={"email": "joao@exemplo.com"})

    assert resposta.status_code == 200
    assert len(chamadas) == 1
    destinatario, assunto, corpo_html = chamadas[0]
    assert destinatario == "joao@exemplo.com"
    assert assunto == auth_service.ASSUNTO_REDEFINICAO_SENHA
    assert "João Lima" in corpo_html
    assert 'href="http://testserver/redefinir-senha/' in corpo_html


def test_solicitar_redefinicao_de_email_inexistente_nao_envia_email(client, db, monkeypatch):
    chamadas = _registrar_chamadas(monkeypatch)
    resposta = client.post("/esqueci-senha", data={"email": "ninguem@exemplo.com"})
    assert resposta.status_code == 200
    assert chamadas == []


def test_falha_no_smtp_e_registrada_no_log_sem_propagar(monkeypatch, caplog):
    async def smtp_fora_do_ar(self, mensagem):
        raise ConnectionRefusedError("SMTP indisponível")

    monkeypatch.setattr(FastMail, "send_message", smtp_fora_do_ar)

    with caplog.at_level(logging.ERROR, logger="app.core.email"):
        enviado = asyncio.run(email.enviar_email("joao@exemplo.com", "Assunto", "<p>corpo</p>"))

    assert enviado is False
    assert "Falha ao enviar e-mail para joao@exemplo.com" in caplog.text


def test_falha_no_smtp_nao_altera_resposta_ao_usuario(client, db, monkeypatch):
    _cadastrar(db)

    async def smtp_fora_do_ar(self, mensagem):
        raise ConnectionRefusedError("SMTP indisponível")

    monkeypatch.setattr(FastMail, "send_message", smtp_fora_do_ar)

    com_falha = client.post("/esqueci-senha", data={"email": "joao@exemplo.com"})
    inexistente = client.post("/esqueci-senha", data={"email": "ninguem@exemplo.com"})
    assert com_falha.status_code == inexistente.status_code == 200
    assert com_falha.text == inexistente.text
