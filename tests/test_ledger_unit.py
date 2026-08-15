import pytest
from sqlalchemy import func, select

from app.ledger import (
    MAX_SQLITE_INTEGER,
    DuplicateIdempotencyKeyError,
    InsufficientFundsError,
    InvalidLedgerInputError,
    LedgerCapacityError,
    SessionStateError,
    UnbalancedTransactionError,
    create_account,
    get_balance,
    post_transaction,
    reverse_transaction,
)
from app.models import Account, Entry, Transaction


def pair(db, left_type="liability", right_type="liability", *, allow_negative=False):
    left = create_account(db, "Left", left_type, allow_negative=allow_negative)
    right = create_account(db, "Right", right_type, allow_negative=allow_negative)
    return left, right


def fund_liability(db, wallet, amount=1_000):
    cash = create_account(db, "Platform Cash", "asset")
    post_transaction(
        db,
        "customer deposit",
        [
            {"account_id": cash.id, "direction": "debit", "amount_cents": amount},
            {"account_id": wallet.id, "direction": "credit", "amount_cents": amount},
        ],
    )
    return cash


def entry_count(db):
    return db.scalar(select(func.count()).select_from(Entry))


def test_non_usd_account_is_rejected(db):
    with pytest.raises(InvalidLedgerInputError, match="USD"):
        create_account(db, "Euro wallet", "liability", currency="EUR")


def test_balanced_transaction_updates_liability_balances(db):
    payer, recipient = pair(db)
    fund_liability(db, payer)

    transaction = post_transaction(
        db,
        "payment",
        [
            {"account_id": payer.id, "direction": "debit", "amount_cents": 250},
            {"account_id": recipient.id, "direction": "credit", "amount_cents": 250},
        ],
    )

    assert transaction.id is not None
    assert get_balance(db, payer.id) == 750
    assert get_balance(db, recipient.id) == 250


@pytest.mark.parametrize(
    ("account_type", "increase", "decrease"),
    [
        ("asset", "debit", "credit"),
        ("expense", "debit", "credit"),
        ("liability", "credit", "debit"),
        ("equity", "credit", "debit"),
        ("revenue", "credit", "debit"),
    ],
)
def test_true_normal_balance_semantics(db, account_type, increase, decrease):
    account = create_account(db, account_type, account_type, allow_negative=True)
    offset = create_account(db, f"offset-{account_type}", "equity", allow_negative=True)
    opposite = "credit" if increase == "debit" else "debit"
    post_transaction(
        db,
        "increase",
        [
            {"account_id": account.id, "direction": increase, "amount_cents": 200},
            {"account_id": offset.id, "direction": opposite, "amount_cents": 200},
        ],
    )
    assert get_balance(db, account.id) == 200
    post_transaction(
        db,
        "decrease",
        [
            {"account_id": account.id, "direction": decrease, "amount_cents": 75},
            {
                "account_id": offset.id,
                "direction": "credit" if decrease == "debit" else "debit",
                "amount_cents": 75,
            },
        ],
    )
    assert get_balance(db, account.id) == 125


def test_unbalanced_rejected_without_writes(db):
    left, right = pair(db, allow_negative=True)
    before = entry_count(db)
    with pytest.raises(UnbalancedTransactionError):
        post_transaction(
            db,
            "bad",
            [
                {"account_id": left.id, "direction": "debit", "amount_cents": 10},
                {"account_id": right.id, "direction": "credit", "amount_cents": 9},
            ],
        )
    assert entry_count(db) == before


def test_aggregate_delta_prevents_overdraft_regardless_of_line_order(db):
    payer, recipient = pair(db)
    fund_liability(db, payer, 100)
    before = entry_count(db)
    entries = [
        {"account_id": payer.id, "direction": "debit", "amount_cents": 70},
        {"account_id": recipient.id, "direction": "credit", "amount_cents": 120},
        {"account_id": payer.id, "direction": "debit", "amount_cents": 50},
    ]
    with pytest.raises(InsufficientFundsError):
        post_transaction(db, "too much", entries)
    assert entry_count(db) == before

    with pytest.raises(InsufficientFundsError):
        post_transaction(db, "too much reordered", list(reversed(entries)))
    assert entry_count(db) == before


def test_idempotency_retry_and_payload_conflict(db):
    payer, recipient = pair(db)
    fund_liability(db, payer)
    entries = [
        {"account_id": payer.id, "direction": "debit", "amount_cents": 100},
        {"account_id": recipient.id, "direction": "credit", "amount_cents": 100},
    ]
    first = post_transaction(db, "payment", entries, "payment-1")
    second = post_transaction(db, "payment", entries, "payment-1")
    assert first.id == second.id
    assert db.scalar(
        select(func.count()).select_from(Entry).where(Entry.transaction_id == first.id)
    ) == 2

    with pytest.raises(DuplicateIdempotencyKeyError):
        post_transaction(db, "changed payment", entries, "payment-1")
    assert not db.in_transaction()
    assert get_balance(db, payer.id) == 900


def test_amount_and_cumulative_sqlite_bounds_are_enforced(db):
    left = create_account(db, "Left", "asset", allow_negative=True)
    right = create_account(db, "Right", "equity", allow_negative=True)
    with pytest.raises(InvalidLedgerInputError, match="amount_cents"):
        post_transaction(
            db,
            "too large",
            [
                {"account_id": left.id, "direction": "debit", "amount_cents": MAX_SQLITE_INTEGER + 1},
                {"account_id": right.id, "direction": "credit", "amount_cents": MAX_SQLITE_INTEGER + 1},
            ],
        )

    post_transaction(
        db,
        "at capacity",
        [
            {"account_id": left.id, "direction": "debit", "amount_cents": MAX_SQLITE_INTEGER},
            {"account_id": right.id, "direction": "credit", "amount_cents": MAX_SQLITE_INTEGER},
        ],
    )
    with pytest.raises(LedgerCapacityError, match="capacity"):
        post_transaction(
            db,
            "past capacity",
            [
                {"account_id": left.id, "direction": "debit", "amount_cents": 1},
                {"account_id": right.id, "direction": "credit", "amount_cents": 1},
            ],
        )
    assert get_balance(db, left.id) == MAX_SQLITE_INTEGER


def test_write_helper_rejects_pending_caller_changes(db):
    pending = Account(name="Pending", type="asset", allow_negative=False, currency="USD")
    db.add(pending)
    with pytest.raises(SessionStateError, match="pending caller changes"):
        create_account(db, "Separate", "asset")
    assert pending in db.new


def test_reversal_is_additive_and_zeroes_effect(db):
    payer, recipient = pair(db)
    fund_liability(db, payer)
    original = post_transaction(
        db,
        "payment",
        [
            {"account_id": payer.id, "direction": "debit", "amount_cents": 100},
            {"account_id": recipient.id, "direction": "credit", "amount_cents": 100},
        ],
    )
    original_entry_ids = [entry.id for entry in original.entries]

    reversal = reverse_transaction(db, original.id)

    assert reversal.id != original.id
    assert reversal.reversed_transaction_id == original.id
    assert db.get(Transaction, original.id).status == "reversed"
    assert [entry.id for entry in db.get(Transaction, original.id).entries] == original_entry_ids
    assert get_balance(db, payer.id) == 1_000
    assert get_balance(db, recipient.id) == 0
    assert reverse_transaction(db, original.id).id == reversal.id


def test_failed_reversal_rolls_back_entries_and_metadata(db):
    payer, recipient = pair(db)
    sink = create_account(db, "Sink", "liability")
    fund_liability(db, payer, 100)
    original = post_transaction(
        db,
        "payment",
        [
            {"account_id": payer.id, "direction": "debit", "amount_cents": 100},
            {"account_id": recipient.id, "direction": "credit", "amount_cents": 100},
        ],
    )
    post_transaction(
        db,
        "recipient spends funds",
        [
            {"account_id": recipient.id, "direction": "debit", "amount_cents": 100},
            {"account_id": sink.id, "direction": "credit", "amount_cents": 100},
        ],
    )
    before_entries = entry_count(db)
    before_transactions = db.scalar(select(func.count()).select_from(Transaction))

    with pytest.raises(InsufficientFundsError):
        reverse_transaction(db, original.id)

    assert entry_count(db) == before_entries
    assert db.scalar(select(func.count()).select_from(Transaction)) == before_transactions
    assert db.get(Transaction, original.id).status == "posted"
