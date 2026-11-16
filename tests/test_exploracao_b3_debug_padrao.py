# B3 da exploração de 14/11: sem DEBUG no ambiente o app sobe com DEBUG=False (sem traceback na resposta)
from app.core.config import Settings


def test_debug_e_falso_sem_a_variavel_no_ambiente(monkeypatch):
    monkeypatch.delenv("DEBUG", raising=False)

    assert Settings(_env_file=None).DEBUG is False


def test_debug_ligado_so_quando_pedido(monkeypatch):
    monkeypatch.setenv("DEBUG", "True")

    assert Settings(_env_file=None).DEBUG is True
