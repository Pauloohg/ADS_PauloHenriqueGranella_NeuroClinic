import httpx
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
    ABACATEPAY_WEBHOOK_SECRET: str = ""

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
HORARIOS_ATENDIMENTO = [8, 9, 10, 11, 14, 15, 16, 17]  # hora cheia; 12h e 13h são intervalo comercial; a última sessão termina às 18h
PRAZO_PAGAMENTO_HORAS = 4  # RN09: prazo do Pix e janela para escolher a forma; para comparar datas use plano_service.prazo_pagamento
ANTECEDENCIA_PRIMEIRA_SESSAO_HORAS = PRAZO_PAGAMENTO_HORAS  # RN10: a primeira sessão não pode cair dentro do prazo de pagamento
CARENCIA_REMOCAO_MINUTOS = 60  # RN09: só vale para apagar o plano vencido; o horário é liberado já no fim do prazo
DIAS_UTEIS_VENCIMENTO_BOLETO = 3  # RN09
FOLGA_BOLETO_DIAS_UTEIS = 2  # RN09: o boleto vence no máximo no 2º dia útil antes da primeira sessão, para compensar a tempo
CARENCIA_REMOCAO_BOLETO_DIAS_UTEIS = 1  # RN09: premissa de compensação bancária em até 1 dia útil (a AbacatePay não documenta)

# Pagamento (RF06)
ABACATEPAY_TIMEOUT_SEGUNDOS = 15
MARGEM_EXPIRACAO_PIX_SEGUNDOS = 60
MINIMO_RESTANTE_PIX_SEGUNDOS = 120
INTERVALO_ATUALIZACAO_COBRANCA_SEGUNDOS = 15
INTERVALO_CONFERENCIA_SEGUNDOS = 10  # no máximo uma consulta de conferência por pagamento nesse intervalo
# RNF02: o httpx mede cada fase separadamente; connect + read limita a espera típica a 3 s (pior caso teórico 5 s)
TIMEOUT_CONFERENCIA = httpx.Timeout(connect=1.0, read=2.0, write=1.0, pool=1.0)
# Chave pública fixa da AbacatePay para o HMAC do X-Webhook-Signature (docs.abacatepay.com/pages/webhooks/security); não é segredo
ABACATEPAY_CHAVE_PUBLICA_HMAC = (
    "t9dXRhHHo3yDEj5pVDYz0frf7q6bMKyMRmxxCPIPp3RCplBfXRxqlC6ZpiWmOqj4L63qEaeUOtrCI8P0VMUgo6iIga2ri9ogaHFs0WIIywSMg0q7Rm"
    "Bfybe1E5XJcfC4IW3alNqym0tXoAKkzvfEjZxV6bE0oG2zJrNNYmUCKZyV0KZ3JS8Votf9EAWWYdiDkMkpbMdPggfh1EqHlVkMiTady6jOR3hyzGEHr"
    "Iz2Ret0xHKMbiqkr9HS1JhNHDX9"
)

# Valores fictícios (placeholder) até definirmos os dados reais da clínica
DADOS_CLINICA = {
    "nome": "NeuroClinic",
    "endereco": "Rua Example, 123 — Cidade, UF",
    "telefone": "(00) 0000-0000",
    "horario": "Seg. a sex., 8h às 18h",
}
