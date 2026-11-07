"""plants: company plants/locations; tax is decided by the plant's state

Revision ID: b1c2d3e4f5a6
Revises: 9765bf99cb65
Create Date: 2026-11-04 15:00:00
"""
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = 'b1c2d3e4f5a6'
down_revision = '9765bf99cb65'
branch_labels = None
depends_on = None

# One plant per state to start with; admins can rename them or add more under Master data > Plants.
INITIAL_PLANTS = [
    ("Andhra Pradesh", "37"), ("Chhattisgarh", "22"), ("Goa", "30"), ("Gujarat", "24"), ("Haryana", "06"),
    ("Karnataka", "29"), ("Kerala", "32"), ("Maharashtra", "27"), ("Odisha", "21"), ("Punjab", "03"),
    ("Tamil Nadu", "33"), ("Telangana", "36"), ("West Bengal", "19"),
]


def upgrade() -> None:
    plants = op.create_table(
        'plants',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('state_code', sa.String(length=2), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_by_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['deleted_by_id'], ['users.id'], name=op.f('fk_plants_deleted_by_id_users'), ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_plants')),
    )
    op.create_index(op.f('ix_plants_deleted_at'), 'plants', ['deleted_at'], unique=False)
    op.create_index(op.f('ix_plants_name'), 'plants', ['name'], unique=False)

    op.add_column('sales_orders', sa.Column('plant_id', sa.Integer(), nullable=True))
    op.add_column('sales_orders', sa.Column('plant_name', sa.String(length=200), nullable=True))
    op.add_column('sales_orders', sa.Column('plant_state_code', sa.String(length=2), nullable=True))
    op.create_foreign_key(op.f('fk_sales_orders_plant_id_plants'), 'sales_orders', 'plants', ['plant_id'], ['id'], ondelete='RESTRICT')

    now = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)  # the app stores naive UTC
    op.bulk_insert(plants, [
        {"name": n, "state_code": c, "is_active": True, "created_at": now, "updated_at": now} for n, c in INITIAL_PLANTS
    ])


def downgrade() -> None:
    op.drop_constraint(op.f('fk_sales_orders_plant_id_plants'), 'sales_orders', type_='foreignkey')
    op.drop_column('sales_orders', 'plant_state_code')
    op.drop_column('sales_orders', 'plant_name')
    op.drop_column('sales_orders', 'plant_id')
    op.drop_index(op.f('ix_plants_name'), table_name='plants')
    op.drop_index(op.f('ix_plants_deleted_at'), table_name='plants')
    op.drop_table('plants')
