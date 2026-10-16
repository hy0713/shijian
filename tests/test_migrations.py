from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from shijian.config import settings
from shijian.db import Base


def test_migration_matches_models(monkeypatch, tmp_path):
    # SQLite is only a schema regression fixture; MySQL dialect SQL generation is checked separately.
    url = "sqlite:///" + (tmp_path / "schema.db").as_posix()
    monkeypatch.setenv("DATABASE_URL", url)
    settings.cache_clear()
    try:
        command.upgrade(Config("alembic.ini"), "head")
        engine = create_engine(url)
        with engine.connect() as conn:
            differences = compare_metadata(MigrationContext.configure(conn), Base.metadata)
            assert differences == []
        engine.dispose()
    finally:
        settings.cache_clear()
