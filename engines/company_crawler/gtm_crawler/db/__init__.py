from sqlalchemy import Engine, create_engine

from ..config import DatabaseSettings


def make_engine(settings: DatabaseSettings, pool_size: int | None = None) -> Engine:
    """Engine with a bounded pool and server-side statement and lock timeouts.

    ``pool_size`` overrides the configured size for a process whose concurrency
    needs more (the worker). Timeouts are set per connection, so a runaway
    query or lock wait is cancelled by PostgreSQL itself.
    """
    options = (f"-c statement_timeout={settings.statement_timeout_ms}"
               f" -c lock_timeout={settings.lock_timeout_ms}")  # fmt: skip
    return create_engine(
        settings.url,
        pool_pre_ping=True,
        pool_size=pool_size or settings.pool_size,
        max_overflow=settings.max_overflow,
        pool_timeout=settings.pool_timeout,
        connect_args={"options": options, "connect_timeout": settings.connect_timeout_seconds},
    )
