"""Create the initial Ritsus ledger schema.

Revision ID: 20260815_0001
Revises:
Create Date: 2026-08-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine.reflection import Inspector

revision: str = "20260815_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LEDGER_TABLES = {"accounts", "transactions", "entries"}
EXPECTED_COLUMNS = {
    "accounts": {
        ("id", "INTEGER", False),
        ("name", "VARCHAR", False),
        ("type", "VARCHAR", False),
        ("allow_negative", "BOOLEAN", False),
        ("currency", "VARCHAR", False),
        ("created_at", "DATETIME", False),
    },
    "transactions": {
        ("id", "INTEGER", False),
        ("description", "VARCHAR", False),
        ("status", "VARCHAR", False),
        ("idempotency_key", "VARCHAR", True),
        ("reversed_transaction_id", "INTEGER", True),
        ("created_at", "DATETIME", False),
    },
    "entries": {
        ("id", "INTEGER", False),
        ("transaction_id", "INTEGER", False),
        ("account_id", "INTEGER", False),
        ("direction", "VARCHAR", False),
        ("amount_cents", "INTEGER", False),
        ("created_at", "DATETIME", False),
    },
}
EXPECTED_CHECKS = {
    "accounts": {
        "ck_accounts_type": "type IN ('asset','liability','equity','revenue','expense')",
    },
    "transactions": {
        "ck_transactions_status": "status IN ('posted','reversed')",
    },
    "entries": {
        "ck_entries_direction": "direction IN ('debit','credit')",
        "ck_entries_positive_amount": "amount_cents > 0",
    },
}
EXPECTED_UNIQUES = {
    "accounts": {},
    "transactions": {
        "uq_transactions_direct_reversal": ("reversed_transaction_id",),
        "uq_transactions_idempotency_key": ("idempotency_key",),
    },
    "entries": {},
}
EXPECTED_INDEXES = {
    "accounts": {},
    "transactions": {},
    "entries": {
        "ix_entries_account_id": (("account_id",), False),
        "ix_entries_transaction_id": (("transaction_id",), False),
    },
}
EXPECTED_FOREIGN_KEYS = {
    "accounts": set(),
    "transactions": {
        (("reversed_transaction_id",), "transactions", ("id",)),
    },
    "entries": {
        (("account_id",), "accounts", ("id",)),
        (("transaction_id",), "transactions", ("id",)),
    },
}


def _validate_legacy_schema(inspector: Inspector) -> list[str]:
    errors: list[str] = []
    for table in sorted(LEDGER_TABLES):
        columns = {
            (column["name"], str(column["type"]), column["nullable"])
            for column in inspector.get_columns(table)
        }
        if columns != EXPECTED_COLUMNS[table]:
            errors.append(f"{table} columns")
        if tuple(inspector.get_pk_constraint(table)["constrained_columns"]) != ("id",):
            errors.append(f"{table} primary key")
        checks = {
            constraint["name"]: " ".join(constraint["sqltext"].split())
            for constraint in inspector.get_check_constraints(table)
        }
        if checks != EXPECTED_CHECKS[table]:
            errors.append(f"{table} checks")
        uniques = {
            constraint["name"]: tuple(constraint["column_names"])
            for constraint in inspector.get_unique_constraints(table)
        }
        if uniques != EXPECTED_UNIQUES[table]:
            errors.append(f"{table} unique constraints")
        indexes = {
            index["name"]: (tuple(index["column_names"]), bool(index["unique"]))
            for index in inspector.get_indexes(table)
        }
        if indexes != EXPECTED_INDEXES[table]:
            errors.append(f"{table} indexes")
        foreign_keys = {
            (
                tuple(foreign_key["constrained_columns"]),
                foreign_key["referred_table"],
                tuple(foreign_key["referred_columns"]),
            )
            for foreign_key in inspector.get_foreign_keys(table)
        }
        if foreign_keys != EXPECTED_FOREIGN_KEYS[table]:
            errors.append(f"{table} foreign keys")
    return errors


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing_ledger_tables = set(inspector.get_table_names()) & LEDGER_TABLES
    if existing_ledger_tables:
        if existing_ledger_tables != LEDGER_TABLES:
            raise RuntimeError(
                "cannot migrate a database with a partial Ritsus ledger schema"
            )
        errors = _validate_legacy_schema(inspector)
        if errors:
            raise RuntimeError(
                "cannot adopt an incompatible pre-Alembic Ritsus schema: "
                + ", ".join(errors)
            )
        return

    op.create_table(
        "accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column(
            "allow_negative",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "currency",
            sa.String(),
            nullable=False,
            server_default=sa.text("'USD'"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "type IN ('asset','liability','equity','revenue','expense')",
            name="ck_accounts_type",
        ),
    )
    op.create_table(
        "transactions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column(
            "status",
            sa.String(),
            nullable=False,
            server_default=sa.text("'posted'"),
        ),
        sa.Column("idempotency_key", sa.String(), nullable=True),
        sa.Column("reversed_transaction_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('posted','reversed')", name="ck_transactions_status"
        ),
        sa.ForeignKeyConstraint(
            ["reversed_transaction_id"],
            ["transactions.id"],
        ),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_transactions_idempotency_key"
        ),
        sa.UniqueConstraint(
            "reversed_transaction_id", name="uq_transactions_direct_reversal"
        ),
    )
    op.create_table(
        "entries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("transaction_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("direction", sa.String(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "direction IN ('debit','credit')", name="ck_entries_direction"
        ),
        sa.CheckConstraint("amount_cents > 0", name="ck_entries_positive_amount"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"]),
        sa.ForeignKeyConstraint(["transaction_id"], ["transactions.id"]),
    )
    op.create_index("ix_entries_account_id", "entries", ["account_id"])
    op.create_index("ix_entries_transaction_id", "entries", ["transaction_id"])


def downgrade() -> None:
    op.drop_index("ix_entries_transaction_id", table_name="entries")
    op.drop_index("ix_entries_account_id", table_name="entries")
    op.drop_table("entries")
    op.drop_table("transactions")
    op.drop_table("accounts")
