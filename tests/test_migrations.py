import pytest
from sqlalchemy import create_engine, inspect, text

from app.database import init_db, make_engine, migrate_database, sqlite_url


def test_migration_builds_current_schema(tmp_path):
    database_url = sqlite_url(tmp_path / "migrated.db")

    migrate_database(database_url)
    migrate_database(database_url)

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        assert {"accounts", "transactions", "entries", "alembic_version"} <= set(
            inspector.get_table_names()
        )
        assert {index["name"] for index in inspector.get_indexes("entries")} == {
            "ix_entries_account_id",
            "ix_entries_transaction_id",
        }
        assert {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("transactions")
        } == {
            "uq_transactions_direct_reversal",
            "uq_transactions_idempotency_key",
        }
        with engine.connect() as connection:
            revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
        assert revision == "20260815_0001"
    finally:
        engine.dispose()


def test_migration_adopts_matching_pre_alembic_schema(tmp_path):
    database_url = sqlite_url(tmp_path / "legacy.db")
    engine = make_engine(database_url)
    init_db(engine)
    engine.dispose()

    migrate_database(database_url)

    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
        assert revision == "20260815_0001"
    finally:
        engine.dispose()


def test_migration_rejects_semantically_wrong_named_indexes(tmp_path):
    database_url = sqlite_url(tmp_path / "wrong-indexes.db")
    engine = make_engine(database_url)
    init_db(engine)
    try:
        with engine.begin() as connection:
            connection.execute(text("DROP INDEX ix_entries_account_id"))
            connection.execute(text("DROP INDEX ix_entries_transaction_id"))
            connection.execute(
                text("CREATE INDEX ix_entries_account_id ON entries (amount_cents)")
            )
            connection.execute(
                text("CREATE INDEX ix_entries_transaction_id ON entries (amount_cents)")
            )
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError, match="entries indexes"):
        migrate_database(database_url)


def test_migration_rejects_incompatible_pre_alembic_schema(tmp_path):
    database_url = sqlite_url(tmp_path / "incompatible.db")
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            for table in ("accounts", "transactions", "entries"):
                connection.execute(text(f"CREATE TABLE {table} (garbage INTEGER)"))
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError, match="incompatible pre-Alembic"):
        migrate_database(database_url)

    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            revision_count = connection.scalar(text("SELECT COUNT(*) FROM alembic_version"))
        assert revision_count == 0
    finally:
        engine.dispose()
