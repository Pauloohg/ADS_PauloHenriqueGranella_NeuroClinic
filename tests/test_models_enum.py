import pytest
from sqlalchemy import Enum, text

from app.db.base import Base
from app.models.usuario import TipoUsuario, Usuario

COLUNAS_ENUM = [
    (tabela.name, coluna.name, coluna.type)
    for tabela in Base.metadata.sorted_tables
    for coluna in tabela.columns
    if isinstance(coluna.type, Enum)
]


def test_usuario_grava_valor_do_enum_no_banco(db):
    db.add(Usuario(nome="Maria", email="maria@exemplo.com", senha_hash="x", tipo=TipoUsuario.CLIENTE))
    db.commit()

    salvo = db.execute(text("SELECT tipo FROM usuario WHERE email = 'maria@exemplo.com'")).scalar_one()
    assert salvo == "cliente"

    db.expire_all()
    assert db.query(Usuario).one().tipo is TipoUsuario.CLIENTE


def test_existem_as_colunas_enum_esperadas():
    assert {(t, c) for t, c, _ in COLUNAS_ENUM} >= {
        ("usuario", "tipo"),
        ("plano", "status"),
        ("sessao", "status"),
        ("pagamento", "forma_pagamento"),
        ("pagamento", "status"),
    }


@pytest.mark.parametrize("tabela,coluna,tipo", COLUNAS_ENUM, ids=[f"{t}.{c}" for t, c, _ in COLUNAS_ENUM])
def test_enum_de_coluna_usa_value_e_nao_nome(tabela, coluna, tipo):
    assert tipo.enums == [membro.value for membro in tipo.enum_class]
