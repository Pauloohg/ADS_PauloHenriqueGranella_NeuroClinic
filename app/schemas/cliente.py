from pydantic import BaseModel, EmailStr, Field, field_validator

from app.schemas.usuario import normalizar_nome, validar_telefone


def _digitos_verificadores_cpf(digitos: str) -> str:
    resultado = ""
    for tamanho in (9, 10):
        soma = sum(int(d) * peso for d, peso in zip(digitos[:tamanho], range(tamanho + 1, 1, -1)))
        resto = soma * 10 % 11
        resultado += str(0 if resto == 10 else resto)
    return resultado


def validar_cpf(cpf: str) -> str | None:
    cpf = cpf.strip()
    if not cpf:
        return None
    digitos = "".join(c for c in cpf if c.isdigit())
    if (
        len(digitos) != 11
        or any(c not in "0123456789.- " for c in cpf)
        or len(set(digitos)) == 1
        or _digitos_verificadores_cpf(digitos) != digitos[9:]
    ):
        raise ValueError("Informe um CPF válido.")
    return f"{digitos[:3]}.{digitos[3:6]}.{digitos[6:9]}-{digitos[9:]}"


class PerfilClienteAtualizacao(BaseModel):
    nome: str = Field(min_length=2, max_length=255)
    email: EmailStr
    telefone: str
    cpf: str | None = None

    @field_validator("nome")
    @classmethod
    def _nome(cls, v: str) -> str:
        return normalizar_nome(v)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return v.lower()

    @field_validator("telefone")
    @classmethod
    def _telefone(cls, v: str) -> str:
        return validar_telefone(v)

    @field_validator("cpf")
    @classmethod
    def _cpf(cls, v: str | None) -> str | None:
        return validar_cpf(v or "")
