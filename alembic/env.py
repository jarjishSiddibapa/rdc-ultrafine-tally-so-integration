"""Alembic environment: URL comes from app settings (never from alembic.ini)."""
import os

from alembic import context

from app.core.config import get_settings
from app.core.database import init_engine
from app.models import Base

config = context.config
target_metadata = Base.metadata


def run() -> None:
    # MIGRATE_DB lets tests migrate the test database without touching production data.
    engine = init_engine(os.environ.get("MIGRATE_DB") or None)
    with engine.connect() as conn:
        context.configure(connection=conn, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


run()
