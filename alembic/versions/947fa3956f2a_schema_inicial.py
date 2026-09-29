"""schema inicial

Revision ID: 947fa3956f2a
Revises:
Create Date: 2026-09-29 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '947fa3956f2a'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ENUMS = ['tipo_usuario', 'status_plano', 'status_sessao', 'forma_pagamento_enum', 'status_pagamento']


def upgrade() -> None:
    op.create_table(
        'usuario',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('nome', sa.String(255), nullable=False),
        sa.Column('email', sa.String(255), nullable=False),
        sa.Column('senha_hash', sa.String(255), nullable=False),
        sa.Column('tipo', sa.Enum('cliente', 'funcionario', 'admin', name='tipo_usuario'), nullable=False),
        sa.Column('criado_em', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
        # a revisão 67b77c745e83 remove esta constraint pelo nome — não renomear
        sa.UniqueConstraint('email', name='usuario_email_key'),
    )
    op.create_table(
        'especialidade',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('nome', sa.String(100), nullable=False),
        sa.Column('valor_sessao', sa.Numeric(10, 2), nullable=False),
    )
    op.create_table(
        'cliente',
        sa.Column('usuario_id', sa.Integer(), sa.ForeignKey('usuario.id'), primary_key=True),
        sa.Column('telefone', sa.String(20)),
        # NOT NULL aqui de propósito: a revisão 081cc21873fe é quem torna o cpf nullable
        sa.Column('cpf', sa.String(14), nullable=False, unique=True),
        sa.Column('abacatepay_customer_id', sa.String(50), unique=True),
    )
    op.create_table(
        'funcionario',
        sa.Column('usuario_id', sa.Integer(), sa.ForeignKey('usuario.id'), primary_key=True),
        sa.Column('especialidade_id', sa.Integer(), sa.ForeignKey('especialidade.id'), nullable=False),
        sa.Column('ativo', sa.Boolean(), nullable=False, server_default=sa.text('true')),
    )
    op.create_table(
        'paciente',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('nome', sa.String(255), nullable=False),
        sa.Column('data_nascimento', sa.Date(), nullable=False),
        sa.Column('cliente_id', sa.Integer(), sa.ForeignKey('cliente.usuario_id'), nullable=False),
    )
    op.create_table(
        'plano',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('paciente_id', sa.Integer(), sa.ForeignKey('paciente.id'), nullable=False),
        sa.Column('funcionario_id', sa.Integer(), sa.ForeignKey('funcionario.usuario_id'), nullable=False),
        sa.Column('especialidade_id', sa.Integer(), sa.ForeignKey('especialidade.id'), nullable=False),
        sa.Column('dia_semana', sa.String(50), nullable=False),
        sa.Column('horario', sa.Time(), nullable=False),
        sa.Column('frequencia_semanal', sa.Integer(), nullable=False),
        sa.Column('duracao', sa.String(20), nullable=False),
        sa.Column('valor_total', sa.Numeric(10, 2), nullable=False),
        sa.Column('data_inicio', sa.Date(), nullable=False),
        sa.Column('data_fim', sa.Date(), nullable=False),
        sa.Column('status', sa.Enum('ativo', 'encerrado', name='status_plano'),
                  nullable=False, server_default='ativo'),
        sa.Column('observacao', sa.Text()),
        sa.Column('criado_em', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    )
    op.create_table(
        'sessao',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('plano_id', sa.Integer(), sa.ForeignKey('plano.id'), nullable=False),
        sa.Column('data_hora', sa.DateTime(), nullable=False),
        sa.Column('status', sa.Enum('pendente_pagamento', 'agendada', 'realizada', name='status_sessao'),
                  nullable=False, server_default='pendente_pagamento'),
        sa.Column('foi_remarcada', sa.Boolean(), nullable=False, server_default=sa.text('false')),
    )
    op.create_table(
        'historico_remarcacao',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('sessao_id', sa.Integer(), sa.ForeignKey('sessao.id'), nullable=False),
        sa.Column('data_hora_original', sa.DateTime(), nullable=False),
        sa.Column('data_hora_nova', sa.DateTime(), nullable=False),
        sa.Column('solicitado_em', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    )
    op.create_table(
        'laudo',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('sessao_id', sa.Integer(), sa.ForeignKey('sessao.id'), nullable=False, unique=True),
        sa.Column('arquivo_url', sa.String(255)),
        sa.Column('comparecimento', sa.Boolean(), nullable=False),
        sa.Column('liberado_em', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    )
    op.create_table(
        'pagamento',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('plano_id', sa.Integer(), sa.ForeignKey('plano.id'), nullable=False, unique=True),
        sa.Column('abacatepay_payment_id', sa.String(50), unique=True),
        sa.Column('forma_pagamento', sa.Enum('pix', 'boleto', name='forma_pagamento_enum')),
        sa.Column('status', sa.Enum('pendente', 'confirmado', name='status_pagamento'),
                  nullable=False, server_default='pendente'),
        sa.Column('confirmado_em', sa.DateTime()),
    )


def downgrade() -> None:
    for tabela in ['pagamento', 'laudo', 'historico_remarcacao', 'sessao', 'plano',
                   'paciente', 'funcionario', 'cliente', 'especialidade', 'usuario']:
        op.drop_table(tabela)
    # drop_table não remove os tipos enum do Postgres
    for nome in ENUMS:
        sa.Enum(name=nome).drop(op.get_bind(), checkfirst=True)
