import asyncio
from collections import Counter

from sqlalchemy import func, select

from app.models import Transaction


async def create_account(client, name, account_type):
    response = await client.post(
        "/accounts", json={"name": name, "type": account_type}
    )
    assert response.status_code == 201
    return response.json()


async def test_fifty_request_race_cannot_overdraw(client, session_factory):
    cash = await create_account(client, "Platform Cash", "asset")
    wallet = await create_account(client, "Wallet", "liability")
    sink = await create_account(client, "Sink wallet", "liability")
    funded = await client.post(
        "/transactions",
        json={
            "description": "seed exactly 1000 cents",
            "entries": [
                {"account_id": cash["id"], "direction": "debit", "amount_cents": 1_000},
                {"account_id": wallet["id"], "direction": "credit", "amount_cents": 1_000},
            ],
        },
    )
    assert funded.status_code == 201

    async def spend(index):
        return await client.post(
            "/transactions",
            json={
                "description": f"race withdrawal {index:02d}",
                "entries": [
                    {"account_id": wallet["id"], "direction": "debit", "amount_cents": 100},
                    {"account_id": sink["id"], "direction": "credit", "amount_cents": 100},
                ],
            },
        )

    responses = await asyncio.gather(*(spend(index) for index in range(50)))
    statuses = Counter(response.status_code for response in responses)
    assert statuses == Counter({409: 40, 201: 10})
    assert all(
        response.json().get("error") == "insufficient_funds"
        for response in responses
        if response.status_code == 409
    )
    assert not any(response.status_code == 500 for response in responses)

    wallet_response = await client.get(f"/accounts/{wallet['id']}")
    assert wallet_response.json()["balance_cents"] == 0
    with session_factory() as db:
        successful_race_transactions = db.scalar(
            select(func.count()).select_from(Transaction).where(
                Transaction.description.like("race withdrawal %")
            )
        )
        assert successful_race_transactions == 10

    audit = await client.get("/audit/verify")
    assert audit.status_code == 200
    assert audit.json()["globally_balanced"] is True
    assert audit.json()["negative_balance_violations"] == []
