"""Defense-in-depth verification against the persisted journal itself."""

from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.ledger import MAX_SQLITE_INTEGER, get_balance
from app.models import Account, Entry, Transaction


def run_audit(db: Session) -> dict:
    """Recompute ledger invariants from one consistent SQLite read snapshot."""
    if db.new or db.dirty or db.deleted:
        raise ValueError("audit requires a session with no pending caller changes")
    if db.in_transaction():
        db.rollback()

    db.execute(text("BEGIN"))
    try:
        transaction_rows = db.execute(
            select(Transaction.id, Transaction.reversed_transaction_id)
        ).all()
        transaction_ids = {transaction_id for transaction_id, _reversed_id in transaction_rows}
        orphan_reversal_transactions = [
            transaction_id
            for transaction_id, reversed_id in transaction_rows
            if reversed_id is not None and reversed_id not in transaction_ids
        ]
        account_ids = set(db.scalars(select(Account.id)))
        rows = db.execute(
            select(
                Entry.id,
                Entry.transaction_id,
                Entry.account_id,
                Entry.direction,
                Entry.amount_cents,
            ).order_by(Entry.id)
        ).all()

        total_debits = sum(
            amount for _id, _txn, _account, direction, amount in rows
            if direction == "debit"
        )
        total_credits = sum(
            amount for _id, _txn, _account, direction, amount in rows
            if direction == "credit"
        )

        by_transaction: dict[int, dict[str, int]] = defaultdict(
            lambda: {"debit": 0, "credit": 0, "count": 0}
        )
        orphan_transaction_entries: list[int] = []
        orphan_account_entries: list[int] = []
        invalid_entries: list[int] = []
        out_of_range_entries: list[int] = []
        for entry_id, transaction_id, account_id, direction, amount in rows:
            if transaction_id not in transaction_ids:
                orphan_transaction_entries.append(entry_id)
            if account_id not in account_ids:
                orphan_account_entries.append(entry_id)
            if direction not in {"debit", "credit"} or amount <= 0:
                invalid_entries.append(entry_id)
                continue
            if amount > MAX_SQLITE_INTEGER:
                out_of_range_entries.append(entry_id)
            bucket = by_transaction[transaction_id]
            bucket[direction] += amount
            bucket["count"] += 1

        unbalanced_transactions = [
            transaction_id
            for transaction_id in sorted(transaction_ids)
            if by_transaction[transaction_id]["debit"]
            != by_transaction[transaction_id]["credit"]
        ]
        malformed_transactions = [
            transaction_id
            for transaction_id in sorted(transaction_ids)
            if by_transaction[transaction_id]["count"] < 2
        ]

        negative_balance_violations = []
        accounts = db.scalars(
            select(Account).where(Account.allow_negative.is_(False)).order_by(Account.id)
        ).all()
        for account in accounts:
            balance = get_balance(db, account.id)
            if balance < 0:
                negative_balance_violations.append(
                    {"account_id": account.id, "balance_cents": balance}
                )

        report = {
            "total_debits_cents": total_debits,
            "total_credits_cents": total_credits,
            "globally_balanced": total_debits == total_credits,
            "aggregate_capacity_exceeded": (
                total_debits > MAX_SQLITE_INTEGER or total_credits > MAX_SQLITE_INTEGER
            ),
            "unbalanced_transactions": unbalanced_transactions,
            "malformed_transactions": malformed_transactions,
            "negative_balance_violations": negative_balance_violations,
            "orphan_transaction_entries": orphan_transaction_entries,
            "orphan_account_entries": orphan_account_entries,
            "orphan_reversal_transactions": orphan_reversal_transactions,
            "invalid_entries": invalid_entries,
            "out_of_range_entries": out_of_range_entries,
            "checked_at": datetime.now(timezone.utc),
        }
    finally:
        # Release the explicit snapshot for direct/library callers as well as
        # request-scoped sessions.
        db.rollback()
    return report
