"""Transactional posting engine and all financial business rules."""

from collections import defaultdict
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import Account, Entry, Transaction

DEBIT_NORMAL_TYPES = {"asset", "expense"}
VALID_ACCOUNT_TYPES = DEBIT_NORMAL_TYPES | {"liability", "equity", "revenue"}
VALID_DIRECTIONS = {"debit", "credit"}
MAX_SQLITE_INTEGER = 2**63 - 1
MAX_ENTRIES_PER_TRANSACTION = 100
MAX_DESCRIPTION_LENGTH = 500
MAX_IDEMPOTENCY_KEY_LENGTH = 200


class LedgerError(Exception):
    """Base class for expected ledger failures."""


class UnbalancedTransactionError(LedgerError):
    pass


class InsufficientFundsError(LedgerError):
    pass


class AccountNotFoundError(LedgerError):
    pass


class TransactionNotFoundError(LedgerError):
    pass


class DuplicateIdempotencyKeyError(LedgerError):
    pass


class InvalidLedgerInputError(LedgerError):
    pass


class LedgerCapacityError(InvalidLedgerInputError):
    pass


class SessionStateError(LedgerError):
    pass


def _ensure_no_pending_changes(db: Session) -> None:
    if db.new or db.dirty or db.deleted:
        raise SessionStateError(
            "ledger operations require a session with no pending caller changes"
        )


def _prepare_for_write(db: Session) -> None:
    """Acquire SQLite's write reservation without discarding pending work."""
    _ensure_no_pending_changes(db)
    # SQLAlchemy autobegins for reads. End only that clean snapshot before the
    # ledger operation takes ownership of its write transaction.
    if db.in_transaction():
        db.rollback()
    db.execute(text("BEGIN IMMEDIATE"))


def create_account(
    db: Session,
    name: str,
    type: str,
    allow_negative: bool = False,
    currency: str = "USD",
) -> Account:
    if type not in VALID_ACCOUNT_TYPES:
        raise InvalidLedgerInputError(f"invalid account type: {type}")
    if not name.strip():
        raise InvalidLedgerInputError("account name must not be empty")
    if len(name) > 200:
        raise InvalidLedgerInputError("account name must be at most 200 characters")
    if currency.upper() != "USD":
        raise InvalidLedgerInputError("Ritsus supports USD accounts only")

    try:
        _prepare_for_write(db)
        account = Account(
            name=name,
            type=type,
            allow_negative=allow_negative,
            currency=currency.upper(),
        )
        db.add(account)
        db.flush()
        db.commit()
        return account
    except SessionStateError:
        raise
    except Exception:
        db.rollback()
        raise


def _raw_debit_net(db: Session, account_id: int) -> int:
    # Sum in Python so out-of-band corrupt data cannot trigger SQLite SUM's
    # signed-64-bit overflow before the audit can report it.
    rows = db.execute(
        select(Entry.direction, Entry.amount_cents).where(Entry.account_id == account_id)
    )
    return sum(amount if direction == "debit" else -amount for direction, amount in rows)


def get_balance(db: Session, account_id: int) -> int:
    account = db.get(Account, account_id)
    if account is None:
        raise AccountNotFoundError(f"account {account_id} not found")
    debit_net = _raw_debit_net(db, account_id)
    return debit_net if account.type in DEBIT_NORMAL_TYPES else -debit_net


def _normalize_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(entries) < 2:
        raise InvalidLedgerInputError("a transaction requires at least two entries")
    if len(entries) > MAX_ENTRIES_PER_TRANSACTION:
        raise InvalidLedgerInputError(
            f"a transaction supports at most {MAX_ENTRIES_PER_TRANSACTION} entries"
        )

    normalized: list[dict[str, Any]] = []
    for item in entries:
        try:
            account_id = item["account_id"]
            direction = item["direction"]
            amount_cents = item["amount_cents"]
        except (KeyError, TypeError) as exc:
            raise InvalidLedgerInputError("every entry requires account_id, direction, and amount_cents") from exc
        if not isinstance(account_id, int) or isinstance(account_id, bool):
            raise InvalidLedgerInputError("account_id must be an integer")
        if direction not in VALID_DIRECTIONS:
            raise InvalidLedgerInputError(f"invalid entry direction: {direction}")
        if (
            not isinstance(amount_cents, int)
            or isinstance(amount_cents, bool)
            or amount_cents <= 0
            or amount_cents > MAX_SQLITE_INTEGER
        ):
            raise InvalidLedgerInputError(
                f"amount_cents must be an integer from 1 to {MAX_SQLITE_INTEGER}"
            )
        normalized.append(
            {
                "account_id": account_id,
                "direction": direction,
                "amount_cents": amount_cents,
            }
        )

    total_debits = sum(
        item["amount_cents"] for item in normalized if item["direction"] == "debit"
    )
    total_credits = sum(
        item["amount_cents"] for item in normalized if item["direction"] == "credit"
    )
    if total_debits != total_credits:
        raise UnbalancedTransactionError(
            f"debits ({total_debits}) must equal credits ({total_credits})"
        )
    return normalized


def _same_payload(
    existing: Transaction,
    description: str,
    entries: list[dict[str, Any]],
) -> bool:
    stored = [
        {
            "account_id": entry.account_id,
            "direction": entry.direction,
            "amount_cents": entry.amount_cents,
        }
        for entry in existing.entries
    ]
    return existing.description == description and stored == entries


def _resolve_existing_key(
    db: Session,
    idempotency_key: str,
    description: str,
    entries: list[dict[str, Any]],
) -> Transaction | None:
    existing = db.scalar(
        select(Transaction).where(Transaction.idempotency_key == idempotency_key)
    )
    if existing is None:
        return None
    # Compare the raw request first: an existing key binds to its original
    # payload, even when a later conflicting payload is itself malformed.
    if not _same_payload(existing, description, entries):
        raise DuplicateIdempotencyKeyError(
            "idempotency key was already used with a different payload"
        )
    return existing


def _account_delta(account: Account, direction: str, amount_cents: int) -> int:
    increases = (
        direction == "debit"
        if account.type in DEBIT_NORMAL_TYPES
        else direction == "credit"
    )
    return amount_cents if increases else -amount_cents


def _stage_transaction_locked(
    db: Session,
    description: str,
    entries: list[dict[str, Any]],
    *,
    idempotency_key: str | None = None,
    reversed_transaction_id: int | None = None,
) -> Transaction:
    """Validate balances and stage a transaction while BEGIN IMMEDIATE is held."""
    transaction_total = sum(
        item["amount_cents"] for item in entries if item["direction"] == "debit"
    )
    historical_total = sum(
        db.scalars(
            select(Entry.amount_cents).where(Entry.direction == "debit")
        )
    )
    if historical_total + transaction_total > MAX_SQLITE_INTEGER:
        raise LedgerCapacityError(
            "posting would exceed the ledger's signed 64-bit aggregate capacity"
        )

    account_ids = set(item["account_id"] for item in entries)
    accounts = {
        account.id: account
        for account in db.scalars(select(Account).where(Account.id.in_(account_ids))).all()
    }
    missing = sorted(account_ids - accounts.keys())
    if missing:
        raise AccountNotFoundError(f"account {missing[0]} not found")

    deltas: dict[int, int] = defaultdict(int)
    for item in entries:
        account = accounts[item["account_id"]]
        deltas[account.id] += _account_delta(
            account, item["direction"], item["amount_cents"]
        )

    for account_id, delta in deltas.items():
        account = accounts[account_id]
        if not account.allow_negative:
            current = get_balance(db, account_id)
            projected = current + delta
            if projected < 0:
                raise InsufficientFundsError(
                    f"account {account_id} has {current} cents; projected balance is {projected}"
                )

    transaction = Transaction(
        description=description,
        idempotency_key=idempotency_key,
        reversed_transaction_id=reversed_transaction_id,
    )
    transaction.entries = [Entry(**item) for item in entries]
    db.add(transaction)
    db.flush()
    return transaction


def post_transaction(
    db: Session,
    description: str,
    entries: list[dict[str, Any]],
    idempotency_key: str | None = None,
) -> Transaction:
    """Atomically validate and post one balanced journal transaction."""
    _ensure_no_pending_changes(db)
    try:
        # Optimistic lookup makes completed retries cheap. The second lookup
        # under BEGIN IMMEDIATE remains authoritative for concurrent callers.
        if idempotency_key is not None:
            if not idempotency_key or len(idempotency_key) > MAX_IDEMPOTENCY_KEY_LENGTH:
                raise InvalidLedgerInputError(
                    f"idempotency key must contain 1-{MAX_IDEMPOTENCY_KEY_LENGTH} characters"
                )
            existing = _resolve_existing_key(
                db, idempotency_key, description, entries
            )
            if existing is not None:
                db.commit()
                return existing

        if not description.strip():
            raise InvalidLedgerInputError("description must not be empty")
        if len(description) > MAX_DESCRIPTION_LENGTH:
            raise InvalidLedgerInputError(
                f"description must be at most {MAX_DESCRIPTION_LENGTH} characters"
            )
        normalized = _normalize_entries(entries)

        _prepare_for_write(db)
        if idempotency_key is not None:
            existing = _resolve_existing_key(
                db, idempotency_key, description, entries
            )
            if existing is not None:
                db.commit()
                return existing

        transaction = _stage_transaction_locked(
            db,
            description,
            normalized,
            idempotency_key=idempotency_key,
        )
        db.commit()
        return transaction
    except Exception:
        db.rollback()
        raise


def reverse_transaction(db: Session, transaction_id: int) -> Transaction:
    """Atomically append a direct reversal and update its source metadata."""
    try:
        _prepare_for_write(db)
        original = db.get(Transaction, transaction_id)
        if original is None:
            raise TransactionNotFoundError(f"transaction {transaction_id} not found")

        # Retried reversal requests are naturally idempotent. A reversing
        # transaction itself remains independently reversible by its own id.
        existing_reversal = db.scalar(
            select(Transaction).where(
                Transaction.reversed_transaction_id == transaction_id
            )
        )
        if existing_reversal is not None:
            db.commit()
            return existing_reversal

        flipped = [
            {
                "account_id": entry.account_id,
                "direction": "credit" if entry.direction == "debit" else "debit",
                "amount_cents": entry.amount_cents,
            }
            for entry in original.entries
        ]
        normalized = _normalize_entries(flipped)
        reversal = _stage_transaction_locked(
            db,
            f"Reversal of transaction {original.id}: {original.description}",
            normalized,
            reversed_transaction_id=original.id,
        )
        original.status = "reversed"
        db.flush()
        db.commit()
        return reversal
    except SessionStateError:
        raise
    except Exception:
        db.rollback()
        raise
