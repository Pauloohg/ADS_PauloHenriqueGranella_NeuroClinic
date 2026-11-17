from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from app.core import security
from app.models.cliente import Cliente
from app.routers import admin, agenda, auth, cliente, consultas, historico, inicio, meus_atendidos, webhooks
from app.services import avaliacao_service, pagamento_service
from tests.fabricas import (
    SENHA,
    criar_admin,
    criar_cliente,
    criar_especialidade,
    criar_funcionario,
    criar_paciente,
)

# bcrypt com 4 rounds da SENHA das fábricas: o login das fixtures custa ~1 ms em vez de ~200 ms
HASH_RAPIDO = "$2b$04$SO5pxzl0M7UZpmapoIP/W.pKAnw3LBaYB12dL3jsh9wILUuVb0Lie"
assert security.verificar_senha(SENHA, HASH_RAPIDO), "HASH_RAPIDO precisa ser o hash da SENHA de tests/fabricas.py"

INICIO = datetime(2026, 8, 3, 9, 0)  # segunda-feira, 09:00 no horário local
CPF_VALIDO = "529.982.247-25"
MODULOS_COM_RELOGIO = (admin, agenda, auth, cliente, consultas, historico, inicio, meus_atendidos, webhooks)


@contextmanager
def senha_rapida():
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(security, "gerar_hash_senha", lambda senha: HASH_RAPIDO)
        yield


class Relogio:
    def __init__(self):
        self.agora = INICIO

    def utc(self) -> datetime:
        return (self.agora + timedelta(hours=3)).replace(tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def relogio(monkeypatch) -> Relogio:
    relogio = Relogio()
    for modulo in MODULOS_COM_RELOGIO:
        if hasattr(modulo, "agora_local"):
            monkeypatch.setattr(modulo, "agora_local", lambda: relogio.agora)
        if hasattr(modulo, "agora_utc"):
            monkeypatch.setattr(modulo, "agora_utc", relogio.utc)
    return relogio


@pytest.fixture(autouse=True)
def emails(monkeypatch) -> list[tuple[str, str]]:
    enviados = []

    async def registrar(destinatario, assunto, corpo):
        enviados.append((destinatario, assunto))
        return True

    monkeypatch.setattr(pagamento_service, "enviar_email", registrar)
    monkeypatch.setattr(avaliacao_service, "enviar_email", registrar)
    return enviados


@pytest.fixture
def fono(db):
    return criar_especialidade(db, "Fonoaudiologia", "150.00")


@pytest.fixture
def carla(db, fono):
    with senha_rapida():
        return criar_funcionario(db, fono, email="carla@exemplo.com", nome="Carla Fono")


@pytest.fixture
def bruno(db, fono):
    with senha_rapida():
        return criar_funcionario(db, fono, email="bruno@exemplo.com", nome="Bruno Fono")


@pytest.fixture
def maria(db):
    with senha_rapida():
        usuario = criar_cliente(db, email="maria@exemplo.com", nome="Maria Souza")
    db.get(Cliente, usuario.id).cpf = CPF_VALIDO
    db.commit()
    return usuario


@pytest.fixture
def joana(db):
    with senha_rapida():
        return criar_cliente(db, email="joana@exemplo.com", nome="Joana Lima")


@pytest.fixture
def admin_ana(db):
    with senha_rapida():
        return criar_admin(db, email="admin@exemplo.com")


@pytest.fixture
def pedro(db, maria):
    return criar_paciente(db, maria.id, nome="Pedro Souza")
