from datetime import date

INT4_MAX = 2_147_483_647
DIGITOS_ASCII = frozenset("0123456789")


def somente_digitos(texto: str | None) -> str:
    return "".join(c for c in texto or "" if c in DIGITOS_ASCII)


def ler_inteiro(texto: str | None, minimo: int = 0, maximo: int = INT4_MAX) -> int | None:
    # Nunca usar str.isdigit()/isdecimal(): aceitam "²" e "٣", e int() quebra ou converte o que não devia
    if not isinstance(texto, str) or not texto or any(c not in DIGITOS_ASCII for c in texto):
        return None
    significativos = texto.lstrip("0") or "0"
    if len(significativos) > len(str(maximo)):
        return None
    valor = int(significativos)
    return valor if minimo <= valor <= maximo else None


def inteiro_positivo(texto: str | None, maximo: int = INT4_MAX) -> int | None:
    return ler_inteiro(texto, 1, maximo)


def ler_data_iso(texto: str | None) -> date | None:
    # Só AAAA-MM-DD com dígitos ASCII: date.fromisoformat aceita também "20261102" e semanas ISO ("2026-W45-1")
    if not isinstance(texto, str):
        return None
    partes = texto.strip().split("-")
    if [len(parte) for parte in partes] != [4, 2, 2]:
        return None
    ano, mes, dia = (ler_inteiro(parte) for parte in partes)
    if None in (ano, mes, dia):
        return None
    try:
        return date(ano, mes, dia)
    except ValueError:
        return None


def ler_opcao(texto: str | None, opcoes: tuple[str, ...], padrao: str) -> str:
    return texto if isinstance(texto, str) and texto in opcoes else padrao
