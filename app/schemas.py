"""Pydantic request and response contracts for the HTTP API."""

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

AccountType = Literal["asset", "liability", "equity", "revenue", "expense"]
Direction = Literal["debit", "credit"]


class TimestampedModel(BaseModel):
    @field_validator("created_at", check_fields=False)
    @classmethod
    def normalize_sqlite_timestamp(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class AccountCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    type: AccountType
    allow_negative: bool = False
    currency: str = Field(default="USD", min_length=3, max_length=3)


class AccountOut(TimestampedModel):
    id: int
    name: str
    type: AccountType
    allow_negative: bool
    currency: str
    balance_cents: int
    created_at: datetime


class EntryIn(BaseModel):
    account_id: int = Field(gt=0, le=2**63 - 1, strict=True)
    direction: Direction
    amount_cents: int = Field(gt=0, le=2**63 - 1, strict=True)


class TransactionCreate(BaseModel):
    description: str = Field(min_length=1, max_length=500)
    entries: list[EntryIn] = Field(min_length=2, max_length=100)


class EntryOut(TimestampedModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    account_id: int
    direction: Direction
    amount_cents: int
    created_at: datetime


class TransactionOut(TimestampedModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    description: str
    status: Literal["posted", "reversed"]
    idempotency_key: str | None
    reversed_transaction_id: int | None
    entries: list[EntryOut]
    created_at: datetime
