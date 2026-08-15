import asyncio

from sqlalchemy import func, select

from app.models import Entry, Transaction


async def setup_payment(client):
    accounts = {}
    for name, account_type in [
        ("Cash", "asset"),
        ("Payer", "liability"),
        ("Recipient", "liability"),
    ]:
        response = await client.post(
            "/accounts", json={"name": name, "type": account_type}
        )
        accounts[name] = response.json()
    await client.post(
        "/transactions",
        json={
            "description": "deposit",
            "entries": [
                {"account_id": accounts["Cash"]["id"], "direction": "debit", "amount_cents": 500},
                {"account_id": accounts["Payer"]["id"], "direction": "credit", "amount_cents": 500},
            ],
        },
    )
    before = {
        name: (await client.get(f"/accounts/{account['id']}")).json()["balance_cents"]
        for name, account in accounts.items()
    }
    payment = await client.post(
        "/transactions",
        json={
            "description": "payment",
            "entries": [
                {"account_id": accounts["Payer"]["id"], "direction": "debit", "amount_cents": 200},
                {"account_id": accounts["Recipient"]["id"], "direction": "credit", "amount_cents": 200},
            ],
        },
    )
    return accounts, before, payment.json()


async def test_http_reversal_preserves_history_and_restores_balances(client):
    accounts, before, payment = await setup_payment(client)
    original_entries = payment["entries"]

    reversed_response = await client.post(f"/transactions/{payment['id']}/reverse")
    assert reversed_response.status_code == 201
    reversal = reversed_response.json()
    assert reversal["id"] != payment["id"]
    assert reversal["reversed_transaction_id"] == payment["id"]

    original = (await client.get(f"/transactions/{payment['id']}")).json()
    assert original["status"] == "reversed"
    assert original["entries"] == original_entries
    for name, account in accounts.items():
        balance = (await client.get(f"/accounts/{account['id']}")).json()["balance_cents"]
        assert balance == before[name]

    retry = await client.post(f"/transactions/{payment['id']}/reverse")
    assert retry.json()["id"] == reversal["id"]

    reverse_the_reversal = await client.post(
        f"/transactions/{reversal['id']}/reverse"
    )
    assert reverse_the_reversal.status_code == 201
    assert reverse_the_reversal.json()["reversed_transaction_id"] == reversal["id"]
    assert (await client.post("/transactions/999999/reverse")).status_code == 404


async def test_concurrent_reversal_requests_create_one_direct_reversal(
    client, session_factory
):
    _accounts, _before, payment = await setup_payment(client)
    responses = await asyncio.gather(
        *[client.post(f"/transactions/{payment['id']}/reverse") for _ in range(20)]
    )
    assert {response.status_code for response in responses} == {201}
    assert len({response.json()["id"] for response in responses}) == 1
    with session_factory() as db:
        reversal = db.scalar(
            select(Transaction).where(
                Transaction.reversed_transaction_id == payment["id"]
            )
        )
        assert reversal is not None
        assert db.scalar(
            select(func.count()).select_from(Entry).where(
                Entry.transaction_id == reversal.id
            )
        ) == 2
