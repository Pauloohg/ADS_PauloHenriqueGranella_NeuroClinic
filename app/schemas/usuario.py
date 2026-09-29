import re

from pydantic import BaseModel, EmailStr, Field, field_validator

SENHA_MIN_CARACTERES = 8
SENHA_MAX_BYTES = 72  # limite do bcrypt: bytes além do 72º são ignorados silenciosamente no hash

_TELEFONE_REGEX = re.compile(r"^[0-9()+\-\s]{8,20}$")


def _validar_senha(senha: str) -> str:
    if len(senha) < SENHA_MIN_CARACTERES:
        raise ValueError(f"A senha deve ter pelo menos {SENHA_MIN_CARACTERES} caracteres.")
    if len(senha.encode("utf-8")) > SENHA_MAX_BYTES:
        raise ValueError("A senha é longa demais.")
    return senha


class ClienteCadastro(BaseModel):
    nome: str = Field(min_length=2, max_length=255)
    email: EmailStr
    telefone: str
    senha: str

    @field_validator("nome")
    @classmethod
    def _nome(cls, v: str) -> str:
        v = " ".join(v.split())
        if len(v) < 2:
            raise ValueError("Informe o nome completo.")
        return v

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return v.lower()

    @field_validator("telefone")
    @classmethod
    def _telefone(cls, v: str) -> str:
        v = v.strip()
        if not _TELEFONE_REGEX.match(v) or sum(c.isdigit() for c in v) < 8:
            raise ValueError("Informe um telefone válido, com DDD.")
        return v

    @field_validator("senha")
    @classmethod
    def _senha(cls, v: str) -> str:
        return _validar_senha(v)


class Login(BaseModel):
    email: EmailStr
    senha: str = Field(min_length=1)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return v.lower()


class SolicitacaoRedefinicaoSenha(BaseModel):
    email: EmailStr

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return v.lower()


class ConfirmacaoRedefinicaoSenha(BaseModel):
    token: str = Field(min_length=1)
    nova_senha: str

    @field_validator("nova_senha")
    @classmethod
    def _nova_senha(cls, v: str) -> str:
        return _validar_senha(v)
