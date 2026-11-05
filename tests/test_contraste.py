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


# --- Tela inicial do Profissional (redesenho de 05/11) ---------------------------------------------

def _cor(valor: str) -> str:
    valor = valor.strip()
    if valor.startswith("var("):
        return _cor(_variavel(re.match(r"var\(--([\w-]+)\)", valor).group(1)))
    return "#FFFFFF" if valor.lower() == "#fff" else valor


def _cores_da_regra(seletor: str) -> tuple[str, str]:
    corpo = re.search(rf"(?m)^{re.escape(seletor)}\s*\{{([^}}]*)\}}", CSS).group(1)
    fundo = re.search(r"background-color:\s*([^;]+);", corpo).group(1)
    texto = re.search(r"(?<!-)color:\s*([^;]+);", corpo).group(1)
    return _cor(fundo), _cor(texto)


@pytest.mark.parametrize("seletor", [
    ".linha-tempo-agora",  # texto branco sobre o verde escuro da consulta em andamento
    ".selo-em-andamento",  # selo claro dentro do bloco
    ".inicio-prof-aviso",  # cartão âmbar de consultas esperando registro
    ".status-pendente",  # selo "Aguardando registro" na linha do tempo
    ".linha-tempo-consulta",  # consulta comum sobre o creme
    ".inicio-prof-sem-atendimento",
])
def test_combinacoes_da_tela_inicial_do_profissional_tem_contraste_de_4_5(seletor):
    fundo, texto = _cores_da_regra(seletor)
    assert razao_contraste(texto, fundo) >= 4.5


def test_selo_em_andamento_se_destaca_do_bloco_verde():
    fundo_bloco, _ = _cores_da_regra(".linha-tempo-agora")
    fundo_selo, _ = _cores_da_regra(".selo-em-andamento")
    assert razao_contraste(fundo_selo, fundo_bloco) >= 3


def test_borda_da_especialidade_aparece_no_bloco_em_andamento():
    fundo_bloco, _ = _cores_da_regra(".linha-tempo-agora")
    for especialidade in ("psicopedagogia", "fonoaudiologia", "psicologia"):
        assert razao_contraste(_variavel(f"cor-{especialidade}"), fundo_bloco) >= 1.5
