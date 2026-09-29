import hashlib
import hmac
from datetime import datetime, timedelta

import jwt
from passlib.context import CryptContext

from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

FINALIDADE_ACESSO = "acesso"
FINALIDADE_REDEFINICAO = "redefinicao_senha"


class TokenInvalido(Exception):
    pass


class TokenExpirado(TokenInvalido):
    pass


# --- Senha ---------------------------------------------------------------------------------------

def gerar_hash_senha(senha: str) -> str:
    return pwd_context.hash(senha)


def verificar_senha(senha: str, senha_hash: str) -> bool:
    return pwd_context.verify(senha, senha_hash)


def impressao_senha(senha_hash: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), senha_hash.encode(), hashlib.sha256).hexdigest()[:16]


# --- JWT -----------------------------------------------------------------------------------------

def _codificar(payload: dict, agora: datetime, expira_em_minutos: int) -> str:
    payload = {**payload, "iat": agora, "exp": agora + timedelta(minutes=expira_em_minutos)}
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def _decodificar(token: str, finalidade: str, agora: datetime) -> dict:
    # Não reativar verify_exp: o PyJWT compararia com o relógio real em vez de `agora` (RNF14)
    try:
        payload = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM], options={"verify_exp": False}
        )
    except jwt.PyJWTError as exc:
        raise TokenInvalido from exc

    if payload.get("finalidade") != finalidade or "sub" not in payload or "exp" not in payload:
        raise TokenInvalido
    if agora.timestamp() >= payload["exp"]:
        raise TokenExpirado
    return payload


def criar_token_acesso(usuario_id: int, tipo: str, agora: datetime) -> str:
    return _codificar(
        {"sub": str(usuario_id), "tipo": tipo, "finalidade": FINALIDADE_ACESSO},
        agora,
        settings.ACCESS_TOKEN_EXPIRE_MINUTES,
    )


def decodificar_token_acesso(token: str, agora: datetime) -> dict:
    return _decodificar(token, FINALIDADE_ACESSO, agora)


def criar_token_redefinicao(usuario_id: int, senha_hash: str, agora: datetime) -> str:
    return _codificar(
        {"sub": str(usuario_id), "fp": impressao_senha(senha_hash), "finalidade": FINALIDADE_REDEFINICAO},
        agora,
        settings.RESET_TOKEN_EXPIRE_MINUTES,
    )


def decodificar_token_redefinicao(token: str, agora: datetime) -> dict:
    return _decodificar(token, FINALIDADE_REDEFINICAO, agora)
