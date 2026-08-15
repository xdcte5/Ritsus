"""Relational model for the append-only ledger journal."""

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        CheckConstraint(
            "type IN ('asset','liability','equity','revenue','expense')",
            name="ck_accounts_type",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)
    allow_negative: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    currency: Mapped[str] = mapped_column(
        String, nullable=False, default="USD", server_default="USD"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )

    entries: Mapped[list["Entry"]] = relationship(back_populates="account")


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        CheckConstraint("status IN ('posted','reversed')", name="ck_transactions_status"),
        UniqueConstraint("idempotency_key", name="uq_transactions_idempotency_key"),
        UniqueConstraint(
            "reversed_transaction_id", name="uq_transactions_direct_reversal"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    description: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="posted", server_default="posted"
    )
    idempotency_key: Mapped[str | None] = mapped_column(String, nullable=True)
    reversed_transaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("transactions.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )

    entries: Mapped[list["Entry"]] = relationship(
        back_populates="transaction",
        order_by="Entry.id",
        cascade="all, save-update",
    )


class Entry(Base):
    __tablename__ = "entries"
    __table_args__ = (
        CheckConstraint("direction IN ('debit','credit')", name="ck_entries_direction"),
        CheckConstraint("amount_cents > 0", name="ck_entries_positive_amount"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    transaction_id: Mapped[int] = mapped_column(
        ForeignKey("transactions.id"), nullable=False, index=True
    )
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id"), nullable=False, index=True
    )
    direction: Mapped[str] = mapped_column(String, nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )

    transaction: Mapped[Transaction] = relationship(back_populates="entries")
    account: Mapped[Account] = relationship(back_populates="entries")
