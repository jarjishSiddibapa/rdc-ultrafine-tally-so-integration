"""tally_outbox.last_attempt_at / first_attempt_at: when each push to Tally was tried

Revision ID: e4f5a6b7c8d9
Revises: d3e4f5a6b7c8
Create Date: 2026-11-07 10:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = 'e4f5a6b7c8d9'
down_revision = 'd3e4f5a6b7c8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('tally_outbox', sa.Column('first_attempt_at', sa.DateTime(), nullable=True))
    op.add_column('tally_outbox', sa.Column('last_attempt_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column('tally_outbox', 'last_attempt_at')
    op.drop_column('tally_outbox', 'first_attempt_at')
