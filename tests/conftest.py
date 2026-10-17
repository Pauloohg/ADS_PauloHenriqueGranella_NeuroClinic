from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from fastapi_mail import FastMail
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registra todas as tabelas no metadata
from app.core.config import settings
from app.db.base import Base
from app.db.session import get_db
from app.main import app as fastapi_app
from app.services import abacatepay_client, pagamento_service
from tests.abacatepay_falsa import CHAVE_API_TESTE, SECRET_WEBHOOK_TESTE, ApiFalsa


@pytest.fixture(autouse=True)
def sem_smtp_real(monkeypatch):
    async def nao_enviar(self, mensagem):
        return None

    monkeypatch.setattr(FastMail, "send_message", nao_enviar)


@pytest.fixture(autouse=True)
def sem_rede_real(monkeypatch):
    # O TestClient usa um transporte próprio, então bloquear o HTTPTransport só corta a rede de verdade
    def bloquear(*args, **kwargs):
        raise RuntimeError("Chamada HTTP real bloqueada nos testes: use a fixture api_falsa")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", bloquear)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", bloquear)
    monkeypatch.setattr(abacatepay_client, "_TRANSPORTE", None)
    monkeypatch.setattr(pagamento_service, "_COBRANCAS", {})


@pytest.fixture
def api_falsa(monkeypatch) -> ApiFalsa:
    api = ApiFalsa()
    monkeypatch.setattr(abacatepay_client, "_TRANSPORTE", api.transporte())
    monkeypatch.setattr(settings, "ABACATEPAY_API_KEY", CHAVE_API_TESTE)
    monkeypatch.setattr(settings, "ABACATEPAY_BASE_URL", "https://api.abacatepay.com/v2")
    monkeypatch.setattr(settings, "ABACATEPAY_WEBHOOK_SECRET", SECRET_WEBHOOK_TESTE)
    return api


@pytest.fixture
def agora() -> datetime:
    return datetime(2026, 3, 10, 14, 0, tzinfo=timezone.utc)


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def client(db):
    fastapi_app.dependency_overrides[get_db] = lambda: db
    with TestClient(fastapi_app) as c:
        yield c
    fastapi_app.dependency_overrides.clear()
