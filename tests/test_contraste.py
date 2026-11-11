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


# --- Agenda do Profissional (refinamento da Semana 8) ----------------------------------------------

def _propriedade(seletor: str, propriedade: str) -> str:
    corpo = re.search(rf"(?m)^{re.escape(seletor)}\s*\{{([^}}]*)\}}", CSS).group(1)
    return _cor(re.search(rf"(?<![\w-]){propriedade}:\s*([^;]+);", corpo).group(1))


@pytest.mark.parametrize("seletor", [
    ".agenda-selo-agendada",  # selo de contorno sobre o branco do cartão
    ".agenda-atraso-destacado",  # "há N dias" a partir de 14 dias
    ".agenda-faixa-livre",  # "12:00 às 14:00, sem atendimento"
    ".agenda-aviso-feriado",
    ".agenda-periodo-dia-hoje",
    ".status-pendente",  # "Aguardando registro"
    ".status-avaliacao",
    ".status-nao-compareceu",
    ".status-inativo",  # "Realizada" e "Feriado nacional"
    ".status-remarcada",
    ".status-ativo",  # "Em andamento" e "Hoje"
])
def test_combinacoes_da_agenda_tem_contraste_de_4_5(seletor):
    fundo, texto = _cores_da_regra(seletor)
    assert razao_contraste(texto, fundo) >= 4.5


def test_contorno_do_selo_agendada_aparece_no_branco_e_no_creme():
    contorno = _cor(re.search(r"var\(--[\w-]+\)", _propriedade(".agenda-selo-agendada", "border").split()[-1]).group(0))
    for fundo in (BRANCO, CREME):
        assert razao_contraste(contorno, fundo) >= 3


@pytest.mark.parametrize("seletor, minimo", [
    (".agenda-estado-em-andamento", 3),
    (".agenda-estado-avaliacao-enviada", 3),
    (".agenda-estado-nao-compareceu", 3),
    # Âmbar 2,96:1; a borda só acompanha o selo com texto, então não carrega informação sozinha (WCAG 1.4.11)
    (".agenda-estado-aguardando-registro", 2.9),
])
def test_borda_de_estado_aparece_no_branco(seletor, minimo):
    assert razao_contraste(_propriedade(seletor, "border-left-color"), BRANCO) >= minimo


def test_borda_padrao_agendada_aparece_no_branco():
    borda = re.search(r"var\(--[\w-]+\)", _propriedade(".agenda-estado", "border-left")).group(0)
    assert razao_contraste(_cor(borda), BRANCO) >= 3


def test_bordas_de_estado_sao_diferentes_entre_si():
    cores = [_propriedade(f".agenda-estado-{e}", "border-left-color")
             for e in ("em-andamento", "aguardando-registro", "avaliacao-enviada", "nao-compareceu")]
    cores.append(_cor(re.search(r"var\(--[\w-]+\)", _propriedade(".agenda-estado", "border-left")).group(0)))
    assert len({c.upper() for c in cores}) == 5


# --- Meus atendidos e detalhe do atendido (refinamento da Semana 8) -----------------------------------

@pytest.mark.parametrize("seletor", [
    ".meus-linha-cabecalho",  # cabeçalho das colunas sobre o creme
    ".meus-resumo .meus-resumo-pendente",  # "N aguardando registro" na faixa-resumo
    ".status-pendente",  # selo "N consultas" da coluna Registro pendente
])
def test_combinacoes_de_meus_atendidos_tem_contraste_de_4_5(seletor):
    fundo, texto = _cores_da_regra(seletor)
    assert razao_contraste(texto, fundo) >= 4.5


def test_link_ver_abaixo_e_destaque_do_plano_tem_contraste_de_4_5():
    fundo_pendente, _ = _cores_da_regra(".meus-resumo .meus-resumo-pendente")
    assert razao_contraste(_propriedade(".meus-resumo-pendente a", "color"), fundo_pendente) >= 4.5
    assert razao_contraste(_propriedade(".meus-plano-progresso strong", "color"), BRANCO) >= 4.5


def test_selo_link_em_foco_ou_hover_continua_legivel():
    corpo = re.search(r"(?m)^\.meus-selo-link:hover,\s*\.meus-selo-link:focus-visible\s*\{([^}]*)\}", CSS).group(1)
    texto = _cor(re.search(r"(?<!-)color:\s*([^;]+);", corpo).group(1))
    fundo, _ = _cores_da_regra(".status-pendente")
    assert razao_contraste(texto, fundo) >= 4.5


@pytest.mark.parametrize("fundo", [BRANCO, CREME])  # creme = linha com o mouse em cima
def test_nome_do_atendido_e_foco_do_link_esticado(fundo):
    assert razao_contraste(_propriedade(".meus-link", "color"), fundo) >= 4.5
    contorno = re.search(r"var\(--[\w-]+\)", _propriedade(".meus-link:focus-visible::after", "outline")).group(0)
    assert razao_contraste(_cor(contorno), fundo) >= 3


def test_borda_ambar_da_linha_pendente():
    borda = _propriedade(".meus-linha-pendente", "border-left-color")
    assert borda.upper() == _propriedade(".agenda-estado-aguardando-registro", "border-left-color").upper()
    # Mesma exceção da Agenda (2,96:1): a borda só acompanha o selo "N consultas", que tem texto
    assert razao_contraste(borda, BRANCO) >= 2.9


# --- Início do Administrador (refinamento da Semana 8) ------------------------------------------------

def test_icone_dos_cadastros_tem_contraste():
    fundo, texto = _cores_da_regra(".admin-inicio-icone")
    assert razao_contraste(texto, fundo) >= 4.5  # ícone acompanha texto, mas fica legível como se fosse texto


def test_link_do_painel_e_numeros_da_faixa_no_branco():
    assert razao_contraste(_propriedade(".admin-inicio-link", "color"), BRANCO) >= 4.5
    assert razao_contraste(_propriedade(".admin-inicio-numeros strong", "color"), BRANCO) >= 4.5


def test_titulo_dos_cadastros_no_creme():
    assert razao_contraste(_propriedade(".admin-inicio-item", "color"), CREME) >= 4.5


# --- Profissionais do Admin (refinamento da Semana 8) --------------------------------------------------

@pytest.mark.parametrize("seletor", [
    ".prof-sem-ativo",  # "Sem profissional ativo." no grupo e na confirmação
    ".prof-trava",  # explicação da especialidade travada (RN07)
    ".prof-selo-desativado",
])
def test_combinacoes_de_profissionais_tem_contraste_de_4_5(seletor):
    fundo, texto = _cores_da_regra(seletor)
    assert razao_contraste(texto, fundo) >= 4.5


def test_tom_neutro_de_especialidade():
    fundo, texto = _propriedade(".esp-neutra", "--esp-fundo"), _propriedade(".esp-neutra", "--esp-texto")
    assert razao_contraste(texto, fundo) >= 4.5  # chip: texto escuro sobre o cinza-esverdeado
    assert razao_contraste(_propriedade(".esp-neutra", "--esp-cor"), BRANCO) >= 3  # borda do grupo


def test_link_desativar_vermelho_e_bloqueado():
    assert razao_contraste(_propriedade(".prof-desativar", "color"), BRANCO) >= 4.5
    # Bloqueado é componente inativo (WCAG 1.4.3 o dispensa); mesmo assim fica acima de 3:1 e a regra está em texto
    assert razao_contraste(_propriedade(".prof-desativar-bloqueado", "color"), BRANCO) >= 3


def test_fundo_da_linha_desativada_e_seletor_travado_com_texto_escuro():
    assert razao_contraste(_propriedade(".prof-nome strong", "color"), _propriedade(".prof-linha-desativada", "background-color")) >= 4.5
    corpo = re.search(r"(?m)^\.prof-form \.form-select:disabled\s*\{([^}]*)\}", CSS).group(1)
    fundo = _cor(re.search(r"background-color:\s*([^;]+);", corpo).group(1))
    texto = _cor(re.search(r"(?<!-)color:\s*([^;]+);", corpo).group(1))
    assert razao_contraste(texto, fundo) >= 4.5
