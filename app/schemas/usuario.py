import re

from pydantic import BaseModel, EmailStr, Field, ValidationInfo, field_validator

SENHA_MIN_CARACTERES = 8
SENHA_MAX_BYTES = 72  # limite do bcrypt: bytes além do 72º são ignorados silenciosamente no hash

_TELEFONE_REGEX = re.compile(r"^[0-9()+\-\s]{8,20}$")


def validar_senha(senha: str) -> str:
    if len(senha) < SENHA_MIN_CARACTERES:
        raise ValueError(f"A senha deve ter pelo menos {SENHA_MIN_CARACTERES} caracteres.")
    if len(senha.encode("utf-8")) > SENHA_MAX_BYTES:
        raise ValueError("A senha é longa demais.")
    return senha


def normalizar_nome(nome: str) -> str:
    nome = " ".join(nome.split())
    if len(nome) < 2:
        raise ValueError("Informe o nome completo.")
    return nome


def validar_telefone(telefone: str) -> str:
    telefone = telefone.strip()
    if not _TELEFONE_REGEX.match(telefone) or sum(c.isdigit() for c in telefone) < 8:
        raise ValueError("Informe um telefone válido, com DDD.")
    return telefone


class ClienteCadastro(BaseModel):
    nome: str = Field(min_length=2, max_length=255)
    email: EmailStr
    telefone: str
    senha: str

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

    @field_validator("senha")
    @classmethod
    def _senha(cls, v: str) -> str:
        return validar_senha(v)


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
        return validar_senha(v)


class AlteracaoSenha(BaseModel):
    senha_atual: str = Field(min_length=1)
    nova_senha: str
    confirmacao_senha: str = Field(min_length=1)

    @field_validator("nova_senha")
    @classmethod
    def _nova_senha(cls, v: str) -> str:
        return validar_senha(v)

    @field_validator("confirmacao_senha")
    @classmethod
    def _confirmacao(cls, v: str, info: ValidationInfo) -> str:
        # Se nova_senha já falhou, ela não está em info.data — o erro dela basta
        if "nova_senha" in info.data and v != info.data["nova_senha"]:
            raise ValueError("A confirmação não confere com a nova senha.")
        return v


class AdminCadastro(BaseModel):
    nome: str = Field(min_length=2, max_length=255)
    email: EmailStr
    senha: str

    @field_validator("nome")
    @classmethod
    def _nome(cls, v: str) -> str:
        return normalizar_nome(v)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return v.lower()

    @field_validator("senha")
    @classmethod
    def _senha(cls, v: str) -> str:
        return validar_senha(v)
