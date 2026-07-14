"""Alembic environment. DATABASE_URL comes from app.config (repo-root .env),
never hard-coded here — same config module the app itself uses (DD §16.3).
"""
from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import config as app_config

# Alembic Config object, provides access to values within alembic.ini.
alembic_config = context.config

if alembic_config.config_file_name is not None:
    fileConfig(alembic_config.config_file_name)

# psycopg3 driver explicitly (DATABASE_URL is a plain postgresql:// URL).
# alembic.ini's [alembic] sqlalchemy.url is left blank for the normal CLI
# path (app.config is the source of truth); callers driving Alembic
# programmatically (e.g. tests) may pre-set it via Config.set_main_option,
# which wins over app.config.
db_url = (alembic_config.get_main_option("sqlalchemy.url") or "").strip()
if not db_url:
    db_url = (app_config.database_url or "").strip()
if db_url.startswith("postgresql://"):
    db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)
alembic_config.set_main_option("sqlalchemy.url", db_url)

# Hand-written migrations (no autogenerate target metadata needed), but wire
# it up anyway so `alembic revision --autogenerate` stays usable later.
target_metadata = None


def run_migrations_offline() -> None:
    """Emit SQL to stdout/--sql output without a live DB connection."""
    url = alembic_config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        alembic_config.get_section(alembic_config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
