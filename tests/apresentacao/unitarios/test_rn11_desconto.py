from decimal import Decimal

from app.services.plano_service import aplicar_desconto

BRUTO = Decimal("1000.00")


def test_rn11_desconto_quinzenal_de_0_por_cento():
    assert aplicar_desconto(BRUTO, "quinzenal") == (0, Decimal("0.00"), Decimal("1000.00"))


def test_rn11_desconto_mensal_de_5_por_cento():
    assert aplicar_desconto(BRUTO, "mensal") == (5, Decimal("50.00"), Decimal("950.00"))


def test_rn11_desconto_bimestral_de_7_por_cento():
    assert aplicar_desconto(BRUTO, "bimestral") == (7, Decimal("70.00"), Decimal("930.00"))


def test_rn11_desconto_anual_de_10_por_cento():
    assert aplicar_desconto(BRUTO, "anual") == (10, Decimal("100.00"), Decimal("900.00"))


def test_rn11_arredondamento_round_half_up_calculado_a_mao():
    # 5% de R$ 900,50 = R$ 45,025: ROUND_HALF_UP sobe para 45,03 (o arredondamento bancário daria 45,02)
    percentual, desconto, total = aplicar_desconto(Decimal("900.50"), "mensal")

    assert (percentual, desconto, total) == (5, Decimal("45.03"), Decimal("855.47"))
