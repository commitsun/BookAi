from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

from app.core.config import settings
from app.core.database import Base

# Import all models so Alembic detects them
import app.models  # noqa: F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Alembic uses psycopg2 (sync) so multi-statement DDL blocks work correctly.
# The app uses asyncpg at runtime — they coexist fine.
_sync_url = settings.database_url.replace(
    "postgresql+asyncpg://", "postgresql+psycopg2://"
)


def run_migrations_offline() -> None:
    context.configure(
        url=_sync_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(_sync_url)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
