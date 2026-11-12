import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine
from sqlalchemy.exc import OperationalError

TEST_DATABASE_URL = os.environ.get(
    "GTM_TEST_DATABASE_URL", "postgresql+psycopg://gtm:gtm@127.0.0.1:5432/gtm_crawler_test"
)
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def alembic_config() -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "gtm_crawler/db/migrations"))
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    cfg.attributes["configure_logger"] = False
    return cfg


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    eng = create_engine(TEST_DATABASE_URL)
    try:
        eng.connect().close()
    except OperationalError as exc:
        pytest.skip(
            f"PostgreSQL not reachable at test URL ({exc.orig}); run `docker compose up -d`"
        )
    cfg = alembic_config()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield eng
    eng.dispose()


@pytest.fixture
def conn(engine: Engine) -> Iterator[Connection]:
    """A connection whose work is rolled back after each test."""
    with engine.connect() as connection:
        tx = connection.begin()
        yield connection
        tx.rollback()
