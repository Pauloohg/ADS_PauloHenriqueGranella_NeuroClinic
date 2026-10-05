from datetime import date

from pydantic import BaseModel, Field, field_validator

from app.schemas.usuario import normalizar_nome


class PacienteDados(BaseModel):
    nome: str = Field(min_length=2, max_length=255)
    data_nascimento: date

    @field_validator("nome")
    @classmethod
    def _nome(cls, v: str) -> str:
        return normalizar_nome(v)
