import asyncio

from sqlalchemy import func, select

from app.models import Entry, Transaction


async def create_funded_wallet(client):
    cash = (
        await client.post("/accounts", json={"name": "Cash", "type": "asset"})
    ).json()
    wallet = (
        await client.post(
            "/accounts", json={"name": "Payer", "type": "liability"}
        )
    ).json()
    recipient = (
        await client.post(
            "/accounts", json={"name": "Recipient", "type": "liability"}
        )
    ).json()
    await client.post(
        "/transactions",
        json={
            "description": "deposit",
            "entries": [
                {"account_id": cash["id"], "direction": "debit", "amount_cents": 500},
                {"account_id": wallet["id"], "direction": "credit", "amount_cents": 500},
            ],
        },
    )
    return wallet, recipient


async def test_http_idempotency_applies_effect_once(client, session_factory):
    wallet, recipient = await create_funded_wallet(client)
    payload = {
        "description": "payment",
        "entries": [
            {"account_id": wallet["id"], "direction": "debit", "amount_cents": 125},
            {"account_id": recipient["id"], "direction": "credit", "amount_cents": 125},
        ],
    }
    headers = {"Idempotency-Key": "payment-125"}
    first = await client.post("/transactions", json=payload, headers=headers)
    second = await client.post("/transactions", json=payload, headers=headers)

    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert (await client.get(f"/accounts/{wallet['id']}")).json()["balance_cents"] == 375
    with session_factory() as db:
        assert db.scalar(
            select(func.count()).select_from(Entry).where(
                Entry.transaction_id == first.json()["id"]
            )
        ) == 2

    conflict = await client.post(
        "/transactions",
        json={**payload, "description": "different payment"},
        headers=headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"] == "idempotency_conflict"

    changed_entries = {
        **payload,
        "entries": [
            {**payload["entries"][0], "amount_cents": 124},
            {**payload["entries"][1], "amount_cents": 124},
        ],
    }
    entry_conflict = await client.post(
        "/transactions", json=changed_entries, headers=headers
    )
    assert entry_conflict.status_code == 409
    assert entry_conflict.json()["error"] == "idempotency_conflict"

    unbalanced_conflict = await client.post(
        "/transactions",
        json={
            **payload,
            "entries": [payload["entries"][0], {**payload["entries"][1], "amount_cents": 124}],
        },
        headers=headers,
    )
    assert unbalanced_conflict.status_code == 409
    assert unbalanced_conflict.json()["error"] == "idempotency_conflict"


async def test_concurrent_same_key_uses_one_transaction(client, session_factory):
    wallet, recipient = await create_funded_wallet(client)
    payload = {
        "description": "one logical payment",
        "entries": [
            {"account_id": wallet["id"], "direction": "debit", "amount_cents": 100},
            {"account_id": recipient["id"], "direction": "credit", "amount_cents": 100},
        ],
    }
    responses = await asyncio.gather(
        *[
            client.post(
                "/transactions",
                json=payload,
                headers={"Idempotency-Key": "concurrent-key"},
            )
            for _ in range(20)
        ]
    )
    assert {response.status_code for response in responses} == {201}
    assert len({response.json()["id"] for response in responses}) == 1
    with session_factory() as db:
        assert db.scalar(
            select(func.count()).select_from(Transaction).where(
                Transaction.idempotency_key == "concurrent-key"
            )
        ) == 1
