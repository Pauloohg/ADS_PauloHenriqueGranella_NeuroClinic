"""renomeia laudo para avaliacao

Revision ID: b3d9a1c4e7f2
Revises: e0851272df78
Create Date: 2026-10-23 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = 'b3d9a1c4e7f2'
down_revision: Union[str, None] = 'e0851272df78'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Escrita à mão: o --autogenerate faria drop_table('laudo') + create_table('avaliacao') e apagaria os dados
CONSTRAINTS = ['pkey', 'sessao_id_key', 'sessao_id_fkey']


def _renomear(de: str, para: str) -> None:
    op.rename_table(de, para)
    for sufixo in CONSTRAINTS:
        # Renomear a constraint de PK/UNIQUE renomeia junto o índice que a sustenta
        op.execute(f'ALTER TABLE {para} RENAME CONSTRAINT {de}_{sufixo} TO {para}_{sufixo}')
    op.execute(f'ALTER SEQUENCE {de}_id_seq RENAME TO {para}_id_seq')


def upgrade() -> None:
    _renomear('laudo', 'avaliacao')


def downgrade() -> None:
    _renomear('avaliacao', 'laudo')
