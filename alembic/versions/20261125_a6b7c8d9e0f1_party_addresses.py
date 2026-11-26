"""party_addresses: a customer can have several addresses; orders pick from them

Revision ID: a6b7c8d9e0f1
Revises: f5a6b7c8d9e0
Create Date: 2026-11-25 10:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = 'a6b7c8d9e0f1'
down_revision = 'f5a6b7c8d9e0'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'party_addresses',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('party_id', sa.Integer(), nullable=False),
        sa.Column('label', sa.String(length=100), nullable=False),
        sa.Column('address_line1', sa.String(length=200), nullable=False),
        sa.Column('address_line2', sa.String(length=200), nullable=True),
        sa.Column('city', sa.String(length=100), nullable=True),
        sa.Column('state_code', sa.String(length=2), nullable=True),
        sa.Column('pincode', sa.String(length=6), nullable=True),
        sa.Column('is_default', sa.Boolean(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_by_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['party_id'], ['parties.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['deleted_by_id'], ['users.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_party_addresses_party_id', 'party_addresses', ['party_id'])
    op.create_index('ix_party_addresses_deleted_at', 'party_addresses', ['deleted_at'])

    # Every customer that already had an address keeps it, as its default "Main address".
    op.execute(
        "INSERT INTO party_addresses (party_id, label, address_line1, address_line2, city, state_code, pincode, "
        "is_default, is_active, created_at, updated_at) "
        "SELECT id, 'Main address', address_line1, address_line2, city, state_code, pincode, 1, 1, created_at, updated_at "
        "FROM parties WHERE address_line1 IS NOT NULL AND address_line1 <> ''"
    )

    for col in ('address_line1', 'address_line2', 'city', 'pincode'):
        op.drop_column('parties', col)

    op.add_column('sales_orders', sa.Column('billing_address_id', sa.Integer(), nullable=True))
    op.add_column('sales_orders', sa.Column('shipping_address_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_so_billing_address', 'sales_orders', 'party_addresses', ['billing_address_id'], ['id'], ondelete='RESTRICT')
    op.create_foreign_key('fk_so_shipping_address', 'sales_orders', 'party_addresses', ['shipping_address_id'], ['id'], ondelete='RESTRICT')


def downgrade() -> None:
    op.drop_constraint('fk_so_shipping_address', 'sales_orders', type_='foreignkey')
    op.drop_constraint('fk_so_billing_address', 'sales_orders', type_='foreignkey')
    op.drop_column('sales_orders', 'shipping_address_id')
    op.drop_column('sales_orders', 'billing_address_id')
    op.add_column('parties', sa.Column('pincode', sa.String(length=6), nullable=True))
    op.add_column('parties', sa.Column('city', sa.String(length=100), nullable=True))
    op.add_column('parties', sa.Column('address_line2', sa.String(length=200), nullable=True))
    op.add_column('parties', sa.Column('address_line1', sa.String(length=200), nullable=True))
    op.execute(
        "UPDATE parties p JOIN party_addresses a ON a.party_id = p.id AND a.is_default = 1 AND a.deleted_at IS NULL "
        "SET p.address_line1 = a.address_line1, p.address_line2 = a.address_line2, p.city = a.city, p.pincode = a.pincode"
    )
    op.drop_table('party_addresses')
