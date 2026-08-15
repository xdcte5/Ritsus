"""FastAPI transport layer for Ritsus."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Path, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.audit import run_audit
from app.database import get_db, migrate_database
from app.ledger import (
    AccountNotFoundError,
    DuplicateIdempotencyKeyError,
    InsufficientFundsError,
    InvalidLedgerInputError,
    TransactionNotFoundError,
    UnbalancedTransactionError,
    create_account,
    get_balance,
    post_transaction,
    reverse_transaction,
)
from app.models import Account, Entry, Transaction
from app.schemas import (
    AccountCreate,
    AccountOut,
    EntryOut,
    TransactionCreate,
    TransactionOut,
)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    migrate_database()
    yield


def error_response(status_code: int, error: str, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": error, "detail": detail},
    )


def create_app() -> FastAPI:
    application = FastAPI(title="Ritsus", version="1.0.0", lifespan=lifespan)

    @application.exception_handler(UnbalancedTransactionError)
    def unbalanced_handler(
        _request: Request, exc: UnbalancedTransactionError
    ) -> JSONResponse:
        return error_response(422, "unbalanced_transaction", str(exc))

    @application.exception_handler(InsufficientFundsError)
    def insufficient_handler(
        _request: Request, exc: InsufficientFundsError
    ) -> JSONResponse:
        return error_response(409, "insufficient_funds", str(exc))

    @application.exception_handler(DuplicateIdempotencyKeyError)
    def idempotency_handler(
        _request: Request, exc: DuplicateIdempotencyKeyError
    ) -> JSONResponse:
        return error_response(409, "idempotency_conflict", str(exc))

    @application.exception_handler(AccountNotFoundError)
    def account_missing_handler(
        _request: Request, exc: AccountNotFoundError
    ) -> JSONResponse:
        return error_response(404, "account_not_found", str(exc))

    @application.exception_handler(TransactionNotFoundError)
    def transaction_missing_handler(
        _request: Request, exc: TransactionNotFoundError
    ) -> JSONResponse:
        return error_response(404, "transaction_not_found", str(exc))

    @application.exception_handler(InvalidLedgerInputError)
    def invalid_input_handler(
        _request: Request, exc: InvalidLedgerInputError
    ) -> JSONResponse:
        return error_response(422, "invalid_ledger_input", str(exc))

    @application.post("/accounts", response_model=AccountOut, status_code=201)
    def create_account_endpoint(
        payload: AccountCreate, db: Session = Depends(get_db)
    ) -> AccountOut:
        account = create_account(db, **payload.model_dump())
        return account_output(db, account)

    @application.get("/accounts/{account_id}", response_model=AccountOut)
    def get_account_endpoint(
        account_id: Annotated[int, Path(gt=0, le=2**63 - 1)],
        db: Session = Depends(get_db),
    ) -> AccountOut:
        account = db.get(Account, account_id)
        if account is None:
            raise AccountNotFoundError(f"account {account_id} not found")
        return account_output(db, account)

    @application.get("/accounts/{account_id}/entries", response_model=list[EntryOut])
    def account_entries_endpoint(
        account_id: Annotated[int, Path(gt=0, le=2**63 - 1)],
        db: Session = Depends(get_db),
    ) -> list[Entry]:
        if db.get(Account, account_id) is None:
            raise AccountNotFoundError(f"account {account_id} not found")
        return list(
            db.scalars(
                select(Entry)
                .where(Entry.account_id == account_id)
                .order_by(Entry.created_at, Entry.id)
            ).all()
        )

    @application.post(
        "/transactions", response_model=TransactionOut, status_code=201
    )
    def post_transaction_endpoint(
        payload: TransactionCreate,
        db: Session = Depends(get_db),
        idempotency_key: str | None = Header(
            default=None,
            alias="Idempotency-Key",
            min_length=1,
            max_length=200,
        ),
    ) -> Transaction:
        entries = [entry.model_dump() for entry in payload.entries]
        return post_transaction(
            db,
            payload.description,
            entries,
            idempotency_key=idempotency_key,
        )

    @application.get("/transactions/{transaction_id}", response_model=TransactionOut)
    def get_transaction_endpoint(
        transaction_id: Annotated[int, Path(gt=0, le=2**63 - 1)],
        db: Session = Depends(get_db),
    ) -> Transaction:
        transaction = db.get(Transaction, transaction_id)
        if transaction is None:
            raise TransactionNotFoundError(f"transaction {transaction_id} not found")
        # Materialize relationship inside the request session.
        _ = transaction.entries
        return transaction

    @application.post(
        "/transactions/{transaction_id}/reverse",
        response_model=TransactionOut,
        status_code=201,
    )
    def reverse_transaction_endpoint(
        transaction_id: Annotated[int, Path(gt=0, le=2**63 - 1)],
        db: Session = Depends(get_db),
    ) -> Transaction:
        return reverse_transaction(db, transaction_id)

    @application.get("/audit/verify")
    def audit_endpoint(db: Session = Depends(get_db)) -> JSONResponse:
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
        return JSONResponse(
            status_code=200 if healthy else 500,
            content={
                **report,
                "checked_at": report["checked_at"].isoformat(),
            },
        )

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/health/ready", response_model=None)
    def readiness(db: Session = Depends(get_db)) -> dict[str, str] | JSONResponse:
        try:
            db.execute(text("SELECT 1"))
        except SQLAlchemyError:
            db.rollback()
            return error_response(
                503,
                "database_unavailable",
                "database readiness check failed",
            )
        return {"status": "ready"}

    return application


def account_output(db: Session, account: Account) -> AccountOut:
    return AccountOut(
        id=account.id,
        name=account.name,
        type=account.type,
        allow_negative=account.allow_negative,
        currency=account.currency,
        balance_cents=get_balance(db, account.id),
        created_at=account.created_at,
    )


app = create_app()
