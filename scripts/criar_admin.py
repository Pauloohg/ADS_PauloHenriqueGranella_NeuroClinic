import sys
from datetime import datetime, timezone
from getpass import getpass

from pydantic import ValidationError

import app.models  # noqa: F401 — registra todas as tabelas antes de usar a sessão
from app.db.session import SessionLocal
from app.routers.auth import erros_por_campo
from app.schemas.usuario import AdminCadastro
from app.services import auth_service

ROTULOS = {"nome": "Nome", "email": "E-mail", "senha": "Senha"}


def ler_dados(entrada=input, entrada_senha=getpass) -> AdminCadastro | None:
    nome = entrada("Nome: ")
    email = entrada("E-mail: ")
    senha = entrada_senha("Senha (não aparece ao digitar): ")
    if entrada_senha("Confirme a senha: ") != senha:
        print("As senhas não conferem.")
        return None
    try:
        return AdminCadastro(nome=nome, email=email, senha=senha)
    except ValidationError as exc:
        for campo, mensagem in erros_por_campo(exc.errors()).items():
            print(f"{ROTULOS.get(campo, campo)}: {mensagem}")
        return None


def main() -> int:
    print("Criação de usuário Administrador — NeuroClinic\n")
    dados = ler_dados()
    if dados is None:
        return 1

    with SessionLocal() as db:
        try:
            admin = auth_service.cadastrar_admin(db, dados, datetime.now(timezone.utc))
        except auth_service.EmailJaCadastrado:
            print(f"Já existe um usuário com o e-mail {dados.email}.")
            return 1
        print(f"\nAdmin criado: {admin.nome} <{admin.email}> (id {admin.id}). Já é possível entrar pelo /login.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
