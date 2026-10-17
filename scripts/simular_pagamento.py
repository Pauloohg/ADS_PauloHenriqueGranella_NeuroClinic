import sys

from sqlalchemy import select

import app.models  # noqa: F401 — registra todas as tabelas antes de usar a sessão
from app.db.session import SessionLocal
from app.models.pagamento import Pagamento
from app.services import abacatepay_client
from app.services.abacatepay_client import ErroAbacatePay

USO = "Uso: python -m scripts.simular_pagamento <id do plano | id da cobrança na AbacatePay>"


def resolver_cobranca_id(argumento: str, sessao_banco=SessionLocal) -> str | None:
    if not argumento.isdigit():
        return argumento
    with sessao_banco() as db:
        return db.scalar(select(Pagamento.abacatepay_payment_id).where(Pagamento.plano_id == int(argumento)))


def main(argumentos: list[str], sessao_banco=SessionLocal) -> int:
    if len(argumentos) != 1:
        print(USO)
        return 1
    cobranca_id = resolver_cobranca_id(argumentos[0].strip(), sessao_banco)
    if not cobranca_id:
        print(f"O plano {argumentos[0]} não tem cobrança gerada. Gere a cobrança pela tela de pagamento antes.")
        return 1

    try:
        atual = abacatepay_client.consultar_cobranca(cobranca_id)
        if not atual.dev_mode:
            print(f"Recusado: a cobrança {cobranca_id} não é de Dev mode (devMode diferente de true).")
            return 1
        resultado = abacatepay_client.simular_pagamento(cobranca_id)
    except ErroAbacatePay as exc:
        print(f"Erro: {exc.mensagem}")
        return 1
    if not resultado.dev_mode:
        print(f"Atenção: a resposta da simulação não indicou devMode true (cobrança {cobranca_id}).")
        return 1

    print(f"Cobrança {resultado.id}: status {resultado.status}, valor R$ {resultado.valor_centavos / 100:.2f}, devMode true.")
    print("A confirmação no sistema chega pelo webhook transparent.completed.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
