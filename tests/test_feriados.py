import locale
from datetime import date, datetime

import holidays
import pytest

from app.models.cliente import Cliente
from app.routers import cliente as cliente_router
from app.services.plano_service import eh_feriado, feriados_do_ano, nome_do_feriado
from tests.fabricas import criar_cliente, criar_especialidade, criar_funcionario, criar_paciente, logar

NOMES_2026 = {
    date(2026, 1, 1): "Confraternização Universal",
    date(2026, 4, 3): "Sexta-feira Santa",
    date(2026, 4, 21): "Tiradentes",
    date(2026, 5, 1): "Dia do Trabalhador",
    date(2026, 9, 7): "Independência do Brasil",
    date(2026, 10, 12): "Nossa Senhora Aparecida",
    date(2026, 11, 2): "Finados",
    date(2026, 11, 15): "Proclamação da República",
    date(2026, 11, 20): "Dia Nacional de Zumbi e da Consciência Negra",
    date(2026, 12, 25): "Natal",
}


@pytest.fixture
def locale_em_ingles(monkeypatch):
    for variavel in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        monkeypatch.setenv(variavel, "en_US.UTF-8")
    anterior = locale.setlocale(locale.LC_ALL)
    for nome in ("en_US.UTF-8", "English_United States.1252", "en_US"):
        try:
            locale.setlocale(locale.LC_ALL, nome)
            break
        except locale.Error:
            continue
    feriados_do_ano.cache_clear()
    yield
    locale.setlocale(locale.LC_ALL, anterior)
    feriados_do_ano.cache_clear()


def test_feriados_de_2026_em_portugues():
    assert dict(feriados_do_ano(2026)) == NOMES_2026


@pytest.mark.parametrize("dia, nome", [
    (date(2026, 10, 12), "Nossa Senhora Aparecida"),
    (date(2026, 11, 2), "Finados"),
    (date(2026, 11, 20), "Dia Nacional de Zumbi e da Consciência Negra"),
])
def test_nomes_em_portugues_mesmo_com_locale_em_ingles(locale_em_ingles, dia, nome):
    # Controle: sem language explícito, a biblioteca segue o ambiente e devolve o nome em inglês
    assert holidays.Brazil(years=2026).get(dia) != nome
    assert nome_do_feriado(dia) == nome
    assert dict(feriados_do_ano(2026)) == NOMES_2026


def test_cache_por_ano():
    feriados_do_ano.cache_clear()
    assert feriados_do_ano(2026) is feriados_do_ano(2026)
    feriados_do_ano(2027)
    assert feriados_do_ano.cache_info().misses == 2


def test_eh_feriado_e_nome_de_dia_comum():
    assert eh_feriado(date(2026, 12, 25)) and not eh_feriado(date(2026, 12, 24))
    assert nome_do_feriado(date(2026, 12, 24)) == ""


def test_feriados_opcionais_continuam_fora():
    for dia in (date(2026, 2, 16), date(2026, 2, 17), date(2026, 6, 4)):  # Carnaval e Corpus Christi
        assert not eh_feriado(dia)


def test_tela_de_resumo_e_json_em_portugues_com_locale_em_ingles(client, db, monkeypatch, locale_em_ingles):
    monkeypatch.setattr(cliente_router, "agora_local", lambda: datetime(2026, 10, 8, 10, 0))
    fono = criar_especialidade(db, "Fonoaudiologia", "150.00")
    carla = criar_funcionario(db, fono, email="carla@exemplo.com")
    maria = criar_cliente(db, email="maria@exemplo.com")
    pedro = criar_paciente(db, maria.id)
    db.get(Cliente, maria.id).cpf = "529.982.247-25"
    db.commit()
    logar(client, maria.email)
    url = f"/contratar/{carla.usuario.id}"

    # Segundas às 14h a partir de 08/10/2026: 12/10 e 02/11 são pulados
    json = client.get(f"{url}/simulacao?dias=0&horario=14:00&frequencia=1&duracao=mensal").json()
    assert json["feriados_pulados"] == [
        {"data": "12/10/2026", "nome": "Nossa Senhora Aparecida"},
        {"data": "02/11/2026", "nome": "Finados"},
    ]
    dados = {"paciente_id": str(pedro.id), "celula": ["0-14"], "frequencia_semanal": "1", "duracao": "mensal"}
    html = client.post(url, data=dados).text
    assert "12/10/2026 — Nossa Senhora Aparecida" in html and "02/11/2026 — Finados" in html
    assert "Our Lady" not in html and "All Souls" not in html
