from datetime import datetime

from sqlalchemy import select

from app.models.avaliacao import Avaliacao
from app.models.sessao import Sessao, StatusSessao
from tests.fabricas import criar_plano, logar

PDF = b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\ntrailer << >>\n%%EOF\n"
SESSAO = datetime(2026, 8, 3, 14, 0)


def test_rf08_rf09_rf10_profissional_registra_e_cliente_baixa_a_avaliacao(client, db, relogio, carla, maria, pedro, emails):
    criar_plano(db, pedro, carla, [(SESSAO, StatusSessao.AGENDADA)])
    sessao_id = db.scalar(select(Sessao.id))
    relogio.agora = datetime(2026, 8, 3, 15, 0)  # a sessão das 14h já terminou

    # 1. Profissional vê a consulta na agenda do dia
    logar(client, carla.usuario.email)
    agenda = client.get("/agenda?visao=dia&data=2026-08-03")
    assert agenda.status_code == 200 and "Pedro Souza" in agenda.text

    # 2. Registra o comparecimento com o PDF
    resposta = client.post(
        f"/agenda/sessoes/{sessao_id}/comparecimento", files={"arquivo": ("avaliacao.pdf", PDF, "application/pdf")},
        follow_redirects=False,
    )
    assert resposta.status_code == 303
    assert db.scalar(select(Sessao.status)) == StatusSessao.REALIZADA
    avaliacao = db.scalar(select(Avaliacao))
    assert emails == [(maria.email, "Nova avaliação disponível")]

    # 3. Cliente vê no Histórico e baixa a avaliação
    client.post("/logout")
    logar(client, maria.email)
    historico = client.get(f"/historico?atendido={pedro.id}")
    assert historico.status_code == 200 and f"/avaliacoes/{avaliacao.id}/arquivo" in historico.text

    arquivo = client.get(f"/avaliacoes/{avaliacao.id}/arquivo")
    assert arquivo.status_code == 200 and arquivo.content == PDF
    assert arquivo.headers["content-type"] == "application/pdf"
