# Ritsus

[![CI](https://github.com/xdcte5/Ritsus/actions/workflows/ci.yml/badge.svg)](https://github.com/xdcte5/Ritsus/actions/workflows/ci.yml)

Ritsus is a compact double-entry ledger API built with FastAPI and SQLAlchemy. It records balanced, integer-cent journal transactions, derives account balances from immutable entry history, and verifies persisted ledger invariants on demand.

## Guarantees

- **Balanced transactions:** every posting must contain equal debit and credit totals.
- **Atomic posting:** transaction metadata and all journal entries commit together or roll back together.
- **Concurrency-safe spending:** SQLite writes use `BEGIN IMMEDIATE`, ensuring funds decisions are made from an authoritative snapshot.
- **Payload-bound idempotency:** replaying the same `Idempotency-Key` and payload returns the original transaction; reusing the key with different data returns `409 idempotency_conflict`.
- **Additive reversals:** corrections append compensating entries rather than modifying or deleting original journal entries.
- **Derived balances:** accounts have no stored balance column that can drift from their entries.
- **Self-auditing storage:** `/audit/verify` recomputes arithmetic, transaction shape, references, integer capacity, and nonnegative-balance invariants from persisted data.

## Architecture

```text
HTTP API (FastAPI)
        |
        v
Posting engine (app/ledger.py)
        |
        v
SQLite journal (WAL mode)
        ^
        |
Alembic schema migrations
```

Ritsus follows standard accounting normal balances: debits increase assets and expenses, while credits increase liabilities, equity, and revenue. Custodial customer wallets are represented as platform liabilities.

## Quick start

Python 3.11 or newer is required.

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
python scripts/seed_demo.py
./run.sh
```

Open `http://127.0.0.1:8000/docs` for the interactive OpenAPI interface.

The demo resets `ledger.db`, creates a set of platform and wallet accounts, posts deposits and payments, rejects an overdraft, reverses a transfer, and finishes with a complete ledger audit.

Set `DATABASE_URL` to use another SQLite file:

```bash
DATABASE_URL=sqlite:///./dev.db ./run.sh
```

API startup automatically applies all checked-in Alembic migrations.

## Docker

```bash
docker build -t ritsus .
docker run --rm -p 8000:8000 ritsus
```

The image runs as a non-root user and exposes a readiness-based health check. The command above is ephemeral: removing the container discards its SQLite journal. For persistent use, mount a writable data directory and point `DATABASE_URL` to a database file within it.

## Testing

Install the development dependencies, then run:

```bash
ruff check .
pytest --cov=app --cov-report=term-missing -q
```

The test suite includes:

- direct posting and reversal unit tests;
- HTTP integration and stable error-contract tests;
- raw-SQL corruption and audit-detection tests;
- payload-bound and concurrent idempotency tests;
- Alembic migration, legacy-schema validation, and malformed-schema rejection tests;
- 200 generated balanced and 200 generated unbalanced Hypothesis examples; and
- a 50-request concurrent overspend race using independent HTTP requests and database sessions.

To repeat the concurrency stability gate:

```bash
for i in 1 2 3 4 5; do
  pytest tests/test_concurrency.py -q || exit 1
done
```

GitHub Actions runs the suite on Python 3.11–3.14, enforces lint and an 85% branch-aware coverage floor, validates migration upgrade/drift/downgrade, repeats the concurrency gate, and builds and smoke-tests the non-root container.

## API

| Method | Path | Behavior |
|---|---|---|
| `POST` | `/accounts` | Create a USD account and return its derived balance |
| `GET` | `/accounts/{id}` | Read account metadata and current balance |
| `GET` | `/accounts/{id}/entries` | Read the account's chronological journal entries |
| `POST` | `/transactions` | Atomically post balanced entries; accepts `Idempotency-Key` |
| `GET` | `/transactions/{id}` | Read a transaction and its entries |
| `POST` | `/transactions/{id}/reverse` | Append or return the transaction's direct reversal |
| `GET` | `/audit/verify` | Verify persisted ledger invariants |
| `GET` | `/health` | Process liveness check |
| `GET` | `/health/ready` | Database readiness check |

### Error responses

Expected domain failures use stable JSON responses:

- unbalanced or invalid transactions: `422`;
- insufficient funds or idempotency conflicts: `409`;
- missing accounts or transactions: `404`;
- unhealthy audit: `500`;
- unavailable database readiness check: `503`.

## Design details

### Balances

Balances are calculated by folding an account's journal entries according to its normal balance. This prevents cached state from diverging from the accounting record. Balance reads are O(entries for the account), so large deployments would require a rebuildable snapshot or materialized-balance strategy while retaining entries as the source of truth.

### Write serialization

Every new SQLite posting acquires `BEGIN IMMEDIATE` before reading accounts or balances. Concurrent writers queue, and each funds decision observes previously committed writes. Entry deltas are aggregated by account before funds checks, preventing repeated lines from bypassing the nonnegative-balance rule.

### Reversals

A reversal posts a new transaction with every direction flipped. Original entries remain unchanged. The source transaction's `status` is a mutable projection indicating that a direct reversal exists, and a database uniqueness constraint allows only one direct reversal per transaction.

A reversal can fail if its projected effect would overdraw an account. In that case, no reversal entries or metadata are committed.

### Schema management

API startup runs `alembic upgrade head`. Direct SQLAlchemy metadata creation is reserved for isolated tests and the resettable demo.

The baseline migration adopts a pre-Alembic Ritsus database only after validating its columns, primary and foreign keys, check expressions, unique constraints, and indexes. Partial or incompatible schemas fail migration instead of being silently stamped as current.

## Deployment scope

The current implementation is designed for a single-host SQLite deployment. It does not provide authentication, tenant authorization, multi-currency accounting, externally signed audit checkpoints, or multi-node write coordination.

For multi-process or multi-node writes, the posting path should move to PostgreSQL and lock all touched account rows in deterministic order with `SELECT ... FOR UPDATE` before deriving balances and evaluating projected funds.

## Verification

The current suite contains **39 tests** with **90.78% branch-aware coverage**. The concurrency test has passed five consecutive runs, the deterministic demo finishes with equal 40,000-cent debit and credit totals, and the container smoke test verifies readiness, Docker health, and non-root execution.

## License

Ritsus is available under the [MIT License](LICENSE).
