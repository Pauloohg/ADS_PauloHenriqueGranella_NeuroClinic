from decimal import Decimal, InvalidOperation

from pydantic import BaseModel, Field, field_validator

VALOR_SESSAO_MAXIMO = Decimal("99999999.99")  # limite da coluna Numeric(10, 2)


def converter_valor(valor: str | Decimal) -> Decimal:
    if isinstance(valor, Decimal):
        texto = str(valor)
    else:
        texto = valor.strip().removeprefix("R$").strip()
        if "," in texto:
            texto = texto.replace(".", "").replace(",", ".")
    try:
        numero = Decimal(texto)
    except InvalidOperation:
        raise ValueError("Informe um valor numérico, como 150,00.") from None
    if not numero.is_finite() or numero <= 0:
        raise ValueError("O valor da sessão deve ser maior que zero.")
    if numero > VALOR_SESSAO_MAXIMO:
        raise ValueError("Valor alto demais.")
    if numero != numero.quantize(Decimal("0.01")):
        raise ValueError("Use no máximo duas casas decimais.")
    return numero.quantize(Decimal("0.01"))


class EspecialidadeCadastro(BaseModel):
    nome: str = Field(min_length=2, max_length=100)
    valor_sessao: Decimal

    @field_validator("nome")
    @classmethod
    def _nome(cls, v: str) -> str:
        v = " ".join(v.split())
        if len(v) < 2:
            raise ValueError("Informe o nome da especialidade.")
        return v

    @field_validator("valor_sessao", mode="before")
    @classmethod
    def _valor(cls, v: str | Decimal) -> Decimal:
        return converter_valor(v)


class EspecialidadeValor(BaseModel):
    valor_sessao: Decimal

    @field_validator("valor_sessao", mode="before")
    @classmethod
    def _valor(cls, v: str | Decimal) -> Decimal:
        return converter_valor(v)
