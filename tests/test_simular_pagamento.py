from contextlib import contextmanager

import httpx

from app.models.pagamento import Pagamento, StatusPagamento
from scripts import simular_pagamento
from tests.abacatepay_falsa import cobranca_pix
from tests.fabricas import criar_cliente, criar_especialidade, criar_funcionario, criar_paciente, criar_plano


def _sessao_de_teste(db):
    @contextmanager
    def abrir():
        yield db
    return abrir


def _plano_com_cobranca(db):
    funcionario = criar_funcionario(db, criar_especialidade(db))
    plano = criar_plano(db, criar_paciente(db, criar_cliente(db).id), funcionario, [])
    db.add(Pagamento(plano_id=plano.id, status=StatusPagamento.PENDENTE, abacatepay_payment_id="pix_char_teste123"))
    db.commit()
    return plano


def test_simula_pelo_id_do_plano_em_dev_mode(db, api_falsa, capsys):
    plano = _plano_com_cobranca(db)
    api_falsa.responder("/transparents/list", [cobranca_pix()])
    api_falsa.responder("/transparents/simulate-payment", cobranca_pix(status="PAID"))

    assert simular_pagamento.main([str(plano.id)], _sessao_de_teste(db)) == 0

    [simulacao] = api_falsa.chamadas("/transparents/simulate-payment")
    assert simulacao.method == "POST" and simulacao.url.params["id"] == "pix_char_teste123"
    assert "status PAID" in capsys.readouterr().out


def test_aceita_o_id_da_cobranca_direto(db, api_falsa):
    api_falsa.responder("/transparents/list", [cobranca_pix(id_="pix_char_outro")])
    api_falsa.responder("/transparents/simulate-payment", cobranca_pix(id_="pix_char_outro", status="PAID"))
    assert simular_pagamento.main(["pix_char_outro"], _sessao_de_teste(db)) == 0
    assert api_falsa.chamadas("/transparents/simulate-payment")[0].url.params["id"] == "pix_char_outro"


def test_recusa_cobranca_que_nao_e_de_dev_mode(db, api_falsa, capsys):
    api_falsa.responder("/transparents/list", [cobranca_pix(dev_mode=False)])
    api_falsa.responder("/transparents/simulate-payment", cobranca_pix(status="PAID"))

    assert simular_pagamento.main(["pix_char_teste123"], _sessao_de_teste(db)) == 1

    assert api_falsa.chamadas("/transparents/simulate-payment") == []
    assert "Recusado" in capsys.readouterr().out


def test_recusa_quando_a_resposta_da_simulacao_nao_indica_dev_mode(db, api_falsa, capsys):
    api_falsa.responder("/transparents/list", [cobranca_pix()])
    api_falsa.responder("/transparents/simulate-payment", cobranca_pix(status="PAID", dev_mode=False))
    assert simular_pagamento.main(["pix_char_teste123"], _sessao_de_teste(db)) == 1
    assert "devMode" in capsys.readouterr().out


def test_plano_sem_cobranca_e_erro_da_api(db, api_falsa, capsys):
    assert simular_pagamento.main(["999"], _sessao_de_teste(db)) == 1
    assert "não tem cobrança" in capsys.readouterr().out

    api_falsa.responder("/transparents/list", httpx.Response(401, json={"error": "Token inválido"}))
    assert simular_pagamento.main(["pix_char_teste123"], _sessao_de_teste(db)) == 1
    assert "Erro:" in capsys.readouterr().out


def test_uso_sem_argumento(db, capsys):
    assert simular_pagamento.main([], _sessao_de_teste(db)) == 1
    assert "Uso:" in capsys.readouterr().out
