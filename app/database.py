"""Database engine, migrations, and SQLAlchemy session configuration."""

import os
from collections.abc import Generator
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./ledger.db")


def make_engine(database_url: str = DATABASE_URL) -> Engine:
    """Create an engine with the guarantees required by the configured database."""
    is_sqlite = make_url(database_url).get_backend_name() == "sqlite"
    engine = create_engine(
        database_url,
        connect_args={"check_same_thread": False} if is_sqlite else {},
    )

    if is_sqlite:

        @event.listens_for(engine, "connect")
        def configure_sqlite(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
            if database_url != "sqlite:///:memory:":
                cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    return engine


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db(bind: Engine = engine) -> None:
    """Create tables directly for isolated tests and the resettable demo."""
    Base.metadata.create_all(bind)


def migrate_database(database_url: str = DATABASE_URL) -> None:
    """Upgrade a configured database to the latest checked-in Alembic revision."""
    config = Config(PROJECT_ROOT / "alembic.ini")
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.attributes["database_url"] = database_url
    command.upgrade(config, "head")


def get_db() -> Generator[Session, None, None]:
    """Yield one independent database session per FastAPI request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def sqlite_url(path: Path) -> str:
    """Return an absolute SQLite URL, primarily useful for isolated tests."""
    return f"sqlite:///{path.resolve()}"
