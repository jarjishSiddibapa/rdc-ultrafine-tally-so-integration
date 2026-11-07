"""user_plants: the plants (regions) each sales person may sell from

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-11-06 18:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = 'd3e4f5a6b7c8'
down_revision = 'c2d3e4f5a6b7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'user_plants',
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('plant_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_user_plants_user_id_users'), ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['plant_id'], ['plants.id'], name=op.f('fk_user_plants_plant_id_plants'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('user_id', 'plant_id', name=op.f('pk_user_plants')),
    )


def downgrade() -> None:
    op.drop_table('user_plants')
