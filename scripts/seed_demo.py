#!/usr/bin/env python3
"""Reset ledger.db and run a deterministic payments story."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.audit import run_audit
from app.database import SessionLocal, engine, init_db
from app.ledger import (
    InsufficientFundsError,
    create_account,
    get_balance,
    post_transaction,
    reverse_transaction,
)


def money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}${cents // 100:,}.{cents % 100:02d}"


def main() -> None:
    print("RITSUS — deterministic demo")
    print("Resetting ledger.db (run this before starting the API server).")
    engine.dispose()
    for suffix in ("", "-wal", "-shm"):
        Path(f"ledger.db{suffix}").unlink(missing_ok=True)
    init_db()

    with SessionLocal() as db:
        cash = create_account(db, "Platform Cash", "asset")
        alice = create_account(db, "Wallet: Alice", "liability")
        bob = create_account(db, "Wallet: Bob", "liability")
        merchant = create_account(db, "Wallet: Merchant Co", "liability")
        fees = create_account(db, "Platform Fees", "revenue")
        accounts = [cash, alice, bob, merchant, fees]
        print("Created platform asset, customer liabilities, and fee revenue accounts.")

        post_transaction(
            db,
            "Alice deposit",
            [
                {"account_id": cash.id, "direction": "debit", "amount_cents": 20_000},
                {"account_id": alice.id, "direction": "credit", "amount_cents": 20_000},
            ],
            "demo-alice-deposit",
        )
        post_transaction(
            db,
            "Bob deposit",
            [
                {"account_id": cash.id, "direction": "debit", "amount_cents": 5_000},
                {"account_id": bob.id, "direction": "credit", "amount_cents": 5_000},
            ],
            "demo-bob-deposit",
        )
        print("Funded Alice with $200.00 and Bob with $50.00.")

        transfer = post_transaction(
            db,
            "Alice pays Bob $25",
            [
                {"account_id": alice.id, "direction": "debit", "amount_cents": 2_500},
                {"account_id": bob.id, "direction": "credit", "amount_cents": 2_500},
            ],
            "demo-alice-bob",
        )
        print("Posted Alice → Bob transfer: $25.00.")

        post_transaction(
            db,
            "Alice pays Merchant Co with $3 fee",
            [
                {"account_id": alice.id, "direction": "debit", "amount_cents": 10_000},
                {"account_id": merchant.id, "direction": "credit", "amount_cents": 9_700},
                {"account_id": fees.id, "direction": "credit", "amount_cents": 300},
            ],
            "demo-merchant-payment",
        )
        print("Posted $100.00 payment: Merchant $97.00 + platform fee $3.00.")

        try:
            post_transaction(
                db,
                "Impossible Bob overdraft",
                [
                    {"account_id": bob.id, "direction": "debit", "amount_cents": 999_999},
                    {"account_id": alice.id, "direction": "credit", "amount_cents": 999_999},
                ],
            )
        except InsufficientFundsError as exc:
            print(f"Rejected overdraft as expected (409-equivalent): {exc}")

        reversal = reverse_transaction(db, transfer.id)
        print(
            f"Reversed transaction {transfer.id} with additive transaction {reversal.id}; "
            "Alice/Bob transfer effect is now zero."
        )

        print("\nFINAL BALANCES")
        print("+----------------------+------------+------------+")
        print("| Account              | Type       | Balance    |")
        print("+----------------------+------------+------------+")
        for account in accounts:
            print(
                f"| {account.name:<20} | {account.type:<10} | {money(get_balance(db, account.id)):>10} |"
            )
        print("+----------------------+------------+------------+")

        report = run_audit(db)
        healthy = (
            report["globally_balanced"]
            and not report["aggregate_capacity_exceeded"]
            and not report["unbalanced_transactions"]
            and not report["malformed_transactions"]
            and not report["negative_balance_violations"]
            and not report["orphan_transaction_entries"]
            and not report["orphan_account_entries"]
            and not report["orphan_reversal_transactions"]
            and not report["invalid_entries"]
            and not report["out_of_range_entries"]
        )
        if not healthy:
            raise SystemExit(f"LEDGER AUDIT FAILED: {report}")
        print(
            "\nLEDGER BALANCED ✅ "
            f"total_debits={report['total_debits_cents']} "
            f"total_credits={report['total_credits_cents']}"
        )


if __name__ == "__main__":
    main()
