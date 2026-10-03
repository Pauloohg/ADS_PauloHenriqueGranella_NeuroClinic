from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from fastapi_mail import FastMail
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registra todas as tabelas no metadata
from app.db.base import Base
from app.db.session import get_db
from app.main import app as fastapi_app


@pytest.fixture(autouse=True)
def sem_smtp_real(monkeypatch):
    async def nao_enviar(self, mensagem):
        return None

    monkeypatch.setattr(FastMail, "send_message", nao_enviar)


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
