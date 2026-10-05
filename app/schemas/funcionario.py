from pydantic import BaseModel, EmailStr, Field, field_validator

from app.schemas.usuario import normalizar_nome, validar_senha


class FuncionarioEdicao(BaseModel):
    nome: str = Field(min_length=2, max_length=255)
    especialidade_id: int

    @field_validator("nome")
    @classmethod
    def _nome(cls, v: str) -> str:
        return normalizar_nome(v)


class FuncionarioCadastro(FuncionarioEdicao):
    email: EmailStr
    senha: str

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return v.lower()

    @field_validator("senha")
    @classmethod
    def _senha(cls, v: str) -> str:
        return validar_senha(v)
