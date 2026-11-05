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
