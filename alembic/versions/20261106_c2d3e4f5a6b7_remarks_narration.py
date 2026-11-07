"""orders: separate mandatory remarks; narration becomes auto-generated text

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-11-06 12:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = 'c2d3e4f5a6b7'
down_revision = 'b1c2d3e4f5a6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('sales_orders', sa.Column('remarks', sa.String(length=500), nullable=False, server_default=''))
    op.alter_column('sales_orders', 'narration', existing_type=sa.String(length=500), type_=sa.Text(), existing_nullable=False)


def downgrade() -> None:
    op.alter_column('sales_orders', 'narration', existing_type=sa.Text(), type_=sa.String(length=500), existing_nullable=False)
    op.drop_column('sales_orders', 'remarks')
