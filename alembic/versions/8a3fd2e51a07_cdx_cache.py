"""cdx cache table

Revision ID: 8a3fd2e51a07
Revises: 46755f74e169
Create Date: 2026-05-26 19:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '8a3fd2e51a07'
down_revision: Union[str, None] = '46755f74e169'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'cdx_cache',
        sa.Column('domain', sa.String(length=255), nullable=False),
        sa.Column('match_type', sa.String(length=16), nullable=False),
        sa.Column('rows_json', sa.JSON(), nullable=False),
        sa.Column('bucket_200', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('bucket_3xx', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('bucket_404', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('fetched_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('domain', 'match_type'),
    )
    with op.batch_alter_table('cdx_cache', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_cdx_cache_fetched_at'),
            ['fetched_at'],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table('cdx_cache', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_cdx_cache_fetched_at'))
    op.drop_table('cdx_cache')
