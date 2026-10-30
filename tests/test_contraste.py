import re
from pathlib import Path

import pytest

CSS = (Path(__file__).resolve().parent.parent / "app" / "static" / "css" / "styles.css").read_text(encoding="utf-8")
CREME = "#F6F2EA"
BRANCO = "#FFFFFF"


def _variavel(nome: str) -> str:
    return re.search(rf"--{nome}:\s*(#[0-9A-Fa-f]{{6}});", CSS).group(1)


def _luminancia(cor: str) -> float:
    def canal(valor: int) -> float:
        c = valor / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (int(cor[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * canal(r) + 0.7152 * canal(g) + 0.0722 * canal(b)


def razao_contraste(a: str, b: str) -> float:
    clara, escura = sorted((_luminancia(a), _luminancia(b)), reverse=True)
    return (clara + 0.05) / (escura + 0.05)


def test_formula_confere_com_os_valores_antigos_medidos():
    assert round(razao_contraste("#DEE2E6", CREME), 2) == 1.17
    assert round(razao_contraste("#DC3545", CREME), 2) == 4.05
    assert round(razao_contraste("#000000", BRANCO), 1) == 21.0


def test_creme_do_css_e_o_mesmo_da_conta():
    assert _variavel("cor-fundo-creme").upper() == CREME


@pytest.mark.parametrize("fundo", [CREME, BRANCO])
def test_borda_dos_campos_tem_contraste_minimo_de_3(fundo):
    assert razao_contraste(_variavel("cor-borda-campo"), fundo) >= 3


@pytest.mark.parametrize("fundo", [CREME, BRANCO])
def test_texto_de_erro_tem_contraste_minimo_de_4_5(fundo):
    assert razao_contraste(_variavel("cor-erro"), fundo) >= 4.5


def test_erro_e_borda_usam_as_variaveis_globais():
    assert "--bs-danger: var(--cor-erro);" in CSS
    assert "--bs-form-invalid-color: var(--cor-erro);" in CSS
    assert re.search(r"\.form-control,\s*\.form-select,\s*\.form-check-input\s*\{\s*border-color: var\(--cor-borda-campo\);", CSS)
    r, g, b = (int(_variavel("cor-erro")[i:i + 2], 16) for i in (1, 3, 5))
    assert f"--cor-erro-rgb: {r}, {g}, {b};" in CSS


@pytest.mark.parametrize("fundo", [CREME, BRANCO])
def test_foco_dos_campos_continua_visivel(fundo):
    regra = re.search(r"\.form-control:focus,\s*\.form-select:focus\s*\{([^}]*)\}", CSS).group(1)
    assert "border-color: var(--bs-primary);" in regra
    assert "box-shadow: 0 0 0 0.25rem var(--bs-focus-ring-color);" in regra
    assert razao_contraste(_variavel("bs-primary"), fundo) >= 3
    assert razao_contraste(_variavel("bs-primary"), _variavel("cor-borda-campo")) >= 2  # o foco escurece a borda
