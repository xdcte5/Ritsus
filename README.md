# Ritsus

[![CI](https://github.com/xdcte5/Ritsus/actions/workflows/ci.yml/badge.svg)](https://github.com/xdcte5/Ritsus/actions/workflows/ci.yml)

## What this is

Ritsus is a compact double-entry accounting service for payments-style workflows. It posts balanced, integer-cent journal transactions through FastAPI and can prove its persisted ledger invariants on demand. The implementation favors visible correctness over feature breadth.

## Why it exists

Moving money correctly is not just an API problem: retries, races, partial failures, and corrections all threaten the accounting record. Ritsus explores a foundational payments-infrastructure problem—money must move atomically and provably—without claiming production scale. Its guarantees are exercised through integration, concurrency, corruption, and property-based tests rather than asserted only in documentation.

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

- **Derived balances:** an account has no balance column. Its normal balance is recomputed from entries, so cached state cannot drift from the journal.
- **Serialized writes:** completed retries use a cheap optimistic idempotency lookup; every new posting then acquires `BEGIN IMMEDIATE` and authoritatively rechecks idempotency before account and balance reads. SQLite writers queue rather than make write decisions from stale snapshots.
- **Bound idempotency:** an `Idempotency-Key` returns the original transaction only when stored description and ordered entries match; a different payload receives `409 idempotency_conflict`.
- **Additive reversals:** the application exposes no entry update/delete path. Reversal entries, the direct-reversal link, and the source status projection commit atomically.
- **True normal balances:** debits increase assets/expenses; credits increase liabilities/equity/revenue. Custodial wallets are platform liabilities, so a payment debits the payer wallet and credits recipient liability plus revenue.

## How to run

Python 3.11 or newer is required. API startup applies the checked-in Alembic migrations automatically. The demo resets `ledger.db`, so run it **before** starting the API.

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
python scripts/seed_demo.py
./run.sh
```

Then open `http://127.0.0.1:8000/docs` for the interactive API. The deterministic demo ends with `LEDGER BALANCED ✅` and leaves its database available to the server. Set `DATABASE_URL` to use a different SQLite file, for example `DATABASE_URL=sqlite:///./dev.db ./run.sh`.

For a production-shaped local process without development reload:

```bash
docker build -t ritsus .
docker run --rm -p 8000:8000 ritsus
```

The image runs as a non-root user and creates/migrates its SQLite database on startup. This `--rm` command is an ephemeral demo: removing the container discards its journal. Mount a dedicated writable directory and point `DATABASE_URL` there if persistence is required.

## How to test

```bash
. .venv/bin/activate
ruff check .
pytest --cov=app --cov-report=term-missing -q
```

`.github/workflows/ci.yml` runs the suite on Python 3.11–3.14, enforces lint and 85% branch-aware coverage, repeats the concurrency gate, validates migration upgrade/drift/downgrade, and builds and smoke-tests the non-root container. The focused race proof is `tests/test_concurrency.py`: 50 independent HTTP requests and database sessions compete to spend a 1,000-cent wallet; exactly 10 commit and 40 receive the expected insufficient-funds response. `tests/test_invariants.py` uses Hypothesis for 200 balanced and 200 deliberately unbalanced examples. To repeat the race stability gate:

```bash
for i in 1 2 3 4 5; do pytest tests/test_concurrency.py -v || exit 1; done
```

## API reference

| Method | Path | Behavior |
|---|---|---|
| `POST` | `/accounts` | Create a USD account; returns its derived balance |
| `GET` | `/accounts/{id}` | Read account metadata and live balance |
| `GET` | `/accounts/{id}/entries` | Read the chronological, append-only statement |
| `POST` | `/transactions` | Atomically post balanced entries; accepts `Idempotency-Key` |
| `GET` | `/transactions/{id}` | Read a transaction and its entries |
| `POST` | `/transactions/{id}/reverse` | Append or return the one direct reversal |
| `GET` | `/audit/verify` | Recompute global, transaction, shape, referential, and nonnegative invariants |
| `GET` | `/health` | Cheap process-liveness response |
| `GET` | `/health/ready` | Verify database connectivity; returns `503` when unavailable |

Expected ledger failures have stable JSON shapes. Unbalanced requests return `422`; insufficient funds and idempotency payload conflicts return `409`; missing accounts or transactions return `404`. An unhealthy audit deliberately returns `500`.

## Design decisions and tradeoffs

- **Balances are derived, never cached.** This eliminates a class of drift bugs at the cost of an O(n) per-account journal aggregation. At hundreds of thousands of entries per account, a production design would add rebuildable snapshots or a materialized balance while retaining entries as source of truth.
- **SQLite plus `BEGIN IMMEDIATE` targets one host.** It provides clear serialized-write correctness for this demonstration. `DATABASE_URL` is configurable, but only SQLite behavior is currently tested; a multi-process or multi-node system should move to PostgreSQL and lock touched accounts with `SELECT ... FOR UPDATE`, allowing unrelated accounts to post concurrently.
- **Schema changes are migration-managed.** API startup runs `alembic upgrade head`; the direct `create_all()` helper remains only for isolated tests and the deliberately resettable demo. The baseline migration adopts a pre-Alembic database only after validating its columns, primary/foreign keys, checks, unique constraints, and indexes; incompatible or partial schemas fail startup.
- **Account deltas are aggregated before funds checks.** Repeated lines against one account cannot bypass the nonnegative invariant and behave identically regardless of line order.
- **Journal correction is additive.** The API never mutates or deletes entries; a reversal appends compensating entries. This is application-enforced rather than tamper-proof storage—the audit detects arithmetic, shape, reference, and nonnegative-balance corruption, but a production system would add stricter database permissions or tamper-evident hashes. The original transaction's `status` is a mutable projection recording that a direct reversal exists.
- **One direct reversal is database-enforced.** A retry returns the existing reversal, while that reversal can itself be reversed by targeting its own ID. A reversal can legitimately fail if credited funds have since been spent; no reversal metadata or entries then commit.
- **USD only.** Adding currency labels without per-currency journals or exchange-rate snapshots would make nominal cent totals meaningless, so non-USD account creation is rejected.
- **Application-enforced cross-row invariants.** SQLite cannot express debit-equals-credit across journal rows as a simple constraint. The posting engine enforces it atomically, while the audit detects arithmetic, malformed-transaction, orphan-reference, and forbidden-negative-balance tampering.
- **Bounded integer aggregates.** Individual amounts and cumulative journal totals are constrained to SQLite's signed 64-bit range before commit; audit and balance calculations sum in Python so corrupt out-of-band data is reported rather than crashing SQL aggregation.

## Future work

The highest-value next steps are a PostgreSQL posting path with deterministic account-row locking, a transactional outbox with idempotent consumers, cursor-paginated statements, authentication and tenant authorization, and tamper-evident audit checkpoints. These are deliberately not claimed by the current implementation.

## Validation evidence

Validated locally on Python 3.14.6 (CI is configured for the declared Python 3.11–3.14 matrix). The release-hardening run completed with **39 tests passing**, **90.78% branch-aware coverage**, zero failures, and zero skips; Alembic upgrade/check/downgrade, validated legacy-schema adoption, and malformed-schema rejection passed; the concurrency gate passed five consecutive times; the demo ended with equal 40,000-cent debit and credit totals; and the non-root Docker container returned `{"status":"ready"}`. See the CI workflow and test files for executable claims.
