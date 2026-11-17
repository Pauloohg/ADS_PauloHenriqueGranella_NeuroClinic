from datetime import date, datetime

from app.models.pagamento import FormaPagamento
from app.services.plano_service import prazo_pagamento, vencimento_boleto

CRIADO_EM_UTC = datetime(2026, 8, 3, 12, 0)  # segunda 03/08, 09:00 no horário local


def test_rn09_pix_vence_4_horas_depois_da_criacao_do_plano():
    assert prazo_pagamento(CRIADO_EM_UTC, date(2026, 8, 31), FormaPagamento.PIX) == datetime(2026, 8, 3, 13, 0)


def test_rn09_boleto_vence_em_3_dias_uteis_quando_a_sessao_esta_longe():
    # seg 03/08 + 3 dias úteis = qui 06/08; o 2º dia útil antes da sessão de 31/08 (27/08) é mais tarde
    assert vencimento_boleto(date(2026, 8, 3), date(2026, 8, 31)) == date(2026, 8, 6)
    assert prazo_pagamento(CRIADO_EM_UTC, date(2026, 8, 31), FormaPagamento.BOLETO) == datetime(2026, 8, 6, 23, 59, 59)


def test_rn09_boleto_vence_no_2o_dia_util_antes_da_primeira_sessao_quando_ele_e_menor():
    # 1ª sessão sex 07/08: 1º dia útil antes = qui 06/08, 2º = qua 05/08, que é menor que qui 06/08
    assert vencimento_boleto(date(2026, 8, 3), date(2026, 8, 7)) == date(2026, 8, 5)


def test_rn09_boleto_conta_dias_uteis_pulando_fim_de_semana_e_feriado():
    # qui 03/09: sex 04, (sáb, dom, seg 07/09 feriado), ter 08, qua 09
    assert vencimento_boleto(date(2026, 9, 3), date(2026, 10, 5)) == date(2026, 9, 9)
