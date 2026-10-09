from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Aplicação
    APP_NAME: str = "NeuroClinic"
    DEBUG: bool = True

    # Banco de dados (PostgreSQL)
    DATABASE_URL: str = "postgresql+psycopg2://postgres:postgres@localhost:5432/neuroclinic"

    # Autenticação (RF01)
    SECRET_KEY: str = "troque-esta-chave-antes-de-qualquer-coisa"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 8
    RESET_TOKEN_EXPIRE_MINUTES: int = 30

    # AbacatePay (RF06)
    ABACATEPAY_API_KEY: str = ""
    ABACATEPAY_BASE_URL: str = "https://api.abacatepay.com/v2"

    # E-mail (fastapi-mail)
    MAIL_USERNAME: str = ""
    MAIL_PASSWORD: str = ""
    MAIL_FROM: str = "no-reply@neuroclinic.example"  # fastapi-mail rejeita domínios reservados como .local e .test
    MAIL_SERVER: str = "smtp.gmail.com"
    MAIL_PORT: int = 587

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()

# Agenda da clínica (RF05 / RN08): dias_semana segue datetime.weekday() — 0 = segunda ... 6 = domingo
DURACAO_SESSAO_MINUTOS = 60
DIAS_UTEIS = (0, 1, 2, 3, 4)
HORARIOS_ATENDIMENTO = [8, 9, 10, 11, 14, 15, 16, 17]  # hora cheia; 12h e 13h são almoço; a última sessão termina às 18h
PRAZO_PAGAMENTO_HORAS = 4  # RN09
ANTECEDENCIA_PRIMEIRA_SESSAO_HORAS = PRAZO_PAGAMENTO_HORAS  # RN10: a primeira sessão não pode cair dentro do prazo de pagamento

# Valores fictícios (placeholder) até definirmos os dados reais da clínica
DADOS_CLINICA = {
    "nome": "NeuroClinic",
    "endereco": "Rua Example, 123 — Cidade, UF",
    "telefone": "(00) 0000-0000",
    "horario": "Seg. a sex., 8h às 18h",
}
