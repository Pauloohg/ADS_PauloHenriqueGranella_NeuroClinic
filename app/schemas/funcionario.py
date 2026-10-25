from pydantic import BaseModel, EmailStr, Field, TypeAdapter, ValidationError, field_validator

from app.schemas.usuario import normalizar_nome, validar_senha, validar_telefone

MSG_EMAIL_CONTATO_INVALIDO = "Informe um e-mail válido, como nome@exemplo.com."
CAMPOS_CONTATO = ("telefone_contato", "email_contato", "formacao", "registro_profissional", "apresentacao")
_EMAIL = TypeAdapter(EmailStr)


class ContatoProfissional(BaseModel):
    telefone_contato: str | None = None  # o limite de 20 vem de validar_telefone, a mesma regra do Cliente
    email_contato: str | None = Field(default=None, max_length=120)
    formacao: str | None = Field(default=None, max_length=300)
    registro_profissional: str | None = Field(default=None, max_length=50)
    apresentacao: str | None = Field(default=None, max_length=500)

    @field_validator(*CAMPOS_CONTATO, mode="before")
    @classmethod
    def _vazio_vira_none(cls, v):
        if isinstance(v, str):
            v = v.strip()
        return v or None

    @field_validator("telefone_contato")
    @classmethod
    def _telefone(cls, v: str | None) -> str | None:
        return validar_telefone(v) if v else None

    @field_validator("email_contato")
    @classmethod
    def _email_contato(cls, v: str | None) -> str | None:
        if not v:
            return None
        try:
            return _EMAIL.validate_python(v).lower()
        except ValidationError:
            raise ValueError(MSG_EMAIL_CONTATO_INVALIDO) from None


class FuncionarioEdicao(ContatoProfissional):
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
