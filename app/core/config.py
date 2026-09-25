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
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 8  # 8h de sessão
    RESET_TOKEN_EXPIRE_MINUTES: int = 30       # RF01 — link de redefinição de senha

    # AbacatePay (RF06)
    ABACATEPAY_API_KEY: str = ""
    ABACATEPAY_BASE_URL: str = "https://api.abacatepay.com/v2"

    # E-mail (fastapi-mail)
    MAIL_USERNAME: str = ""
    MAIL_PASSWORD: str = ""
    MAIL_FROM: str = "no-reply@neuroclinic.local"
    MAIL_SERVER: str = "smtp.gmail.com"
    MAIL_PORT: int = 587

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()
