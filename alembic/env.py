from logging.config import fileConfig
from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config
from app.core.config import settings
from app.core.db import Base
from app.models import entities  # noqa: F401 registers tables
from app.models import intelligence  # noqa: F401
config = context.config
config.set_main_option('sqlalchemy.url', settings().database_url.replace('%', '%%'))
if config.config_file_name:
    fileConfig(config.config_file_name)
target_metadata = Base.metadata


def run_migrations_offline():
    context.configure(url=settings().database_url, target_metadata=target_metadata,
                      literal_binds=True, dialect_opts={'paramstyle': 'named'})
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations():
    # statement_cache_size=0: asyncpg caches prepared statements by default, and
    # a cached plan can be resubmitted after DDL changes the catalog mid-transaction
    # (every migration here runs inside one transaction), which has shown up as a
    # CREATE TABLE appearing to execute twice. DDL connections don't need the cache.
    engine = async_engine_from_config(config.get_section(config.config_ini_section, {}),
                                      prefix='sqlalchemy.', poolclass=pool.NullPool,
                                      connect_args={'statement_cache_size': 0})
    async with engine.connect() as conn:
        await conn.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    import asyncio
    asyncio.run(run_async_migrations())
