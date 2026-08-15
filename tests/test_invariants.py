import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import func, select

from app.audit import run_audit
from app.ledger import UnbalancedTransactionError, create_account, post_transaction
from app.models import Entry


@st.composite
def balanced_entry_specs(draw):
    debit_count, credit_count = draw(
        st.sampled_from([(1, 1), (1, 2), (1, 3), (2, 1), (2, 2), (2, 3), (3, 1), (3, 2)])
    )
    total = draw(st.integers(min_value=max(debit_count, credit_count), max_value=100_000))

    def partition(value, count):
        if count == 1:
            return [value]
        cuts = sorted(
            draw(
                st.lists(
                    st.integers(min_value=1, max_value=value - 1),
                    min_size=count - 1,
                    max_size=count - 1,
                    unique=True,
                ).filter(lambda values: len(values) == count - 1)
            )
        )
        points = [0, *cuts, value]
        parts = [points[index + 1] - points[index] for index in range(count)]
        if any(part <= 0 for part in parts):
            # Cuts can only coincide through an invalid partition; ask Hypothesis
            # for a different example rather than weakening amount positivity.
            from hypothesis import assume

            assume(False)
        return parts

    debits = partition(total, debit_count)
    credits = partition(total, credit_count)
    entries = []
    for amount in debits:
        entries.append((draw(st.integers(0, 3)), "debit", amount))
    for amount in credits:
        entries.append((draw(st.integers(0, 3)), "credit", amount))
    return draw(st.permutations(entries))


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(specs=balanced_entry_specs())
def test_generated_balanced_transactions_preserve_audit(db, specs):
    accounts = db.info.get("invariant_accounts")
    if accounts is None:
        accounts = [
            create_account(db, f"Property account {index}", "liability", allow_negative=True)
            for index in range(4)
        ]
        db.info["invariant_accounts"] = accounts
    entries = [
        {
            "account_id": accounts[index].id,
            "direction": direction,
            "amount_cents": amount,
        }
        for index, direction, amount in specs
    ]
    post_transaction(db, "generated balanced transaction", entries)
    report = run_audit(db)
    assert report["globally_balanced"] is True
    assert report["unbalanced_transactions"] == []


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    debit=st.integers(min_value=1, max_value=100_000),
    credit=st.integers(min_value=1, max_value=100_000),
)
def test_generated_unbalanced_transactions_never_write(db, debit, credit):
    from hypothesis import assume

    assume(debit != credit)
    accounts = db.info.get("unbalanced_accounts")
    if accounts is None:
        accounts = [
            create_account(db, "Unbalanced debit", "asset", allow_negative=True),
            create_account(db, "Unbalanced credit", "liability", allow_negative=True),
        ]
        db.info["unbalanced_accounts"] = accounts
    before = db.scalar(select(func.count()).select_from(Entry))
    with pytest.raises(UnbalancedTransactionError):
        post_transaction(
            db,
            "generated invalid transaction",
            [
                {"account_id": accounts[0].id, "direction": "debit", "amount_cents": debit},
                {"account_id": accounts[1].id, "direction": "credit", "amount_cents": credit},
            ],
        )
    assert db.scalar(select(func.count()).select_from(Entry)) == before
