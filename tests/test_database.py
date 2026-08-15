from sqlalchemy import text

from app.database import make_engine


def test_driver_qualified_sqlite_url_keeps_required_pragmas(tmp_path):
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'qualified.db').resolve()}"
    engine = make_engine(database_url)
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("PRAGMA foreign_keys")) == 1
            assert connection.scalar(text("PRAGMA journal_mode")) == "wal"
            assert connection.scalar(text("PRAGMA busy_timeout")) == 30_000
    finally:
        engine.dispose()
