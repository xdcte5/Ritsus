from sqlalchemy import text

from app.audit import run_audit


async def create_account(client, name, account_type, allow_negative=False):
    response = await client.post(
        "/accounts",
        json={
            "name": name,
            "type": account_type,
            "allow_negative": allow_negative,
        },
    )
    assert response.status_code == 201
    return response.json()


async def test_audit_accepts_balanced_fee_split(client):
    cash = await create_account(client, "Cash", "asset")
    payer = await create_account(client, "Payer wallet", "liability")
    merchant = await create_account(client, "Merchant wallet", "liability")
    revenue = await create_account(client, "Fees", "revenue")
    await client.post(
        "/transactions",
        json={
            "description": "deposit",
            "entries": [
                {"account_id": cash["id"], "direction": "debit", "amount_cents": 10_000},
                {"account_id": payer["id"], "direction": "credit", "amount_cents": 10_000},
            ],
        },
    )
    payment = await client.post(
        "/transactions",
        json={
            "description": "payment with fee",
            "entries": [
                {"account_id": payer["id"], "direction": "debit", "amount_cents": 10_000},
                {"account_id": merchant["id"], "direction": "credit", "amount_cents": 9_700},
                {"account_id": revenue["id"], "direction": "credit", "amount_cents": 300},
            ],
        },
    )
    assert payment.status_code == 201

    response = await client.get("/audit/verify")
    assert response.status_code == 200
    report = response.json()
    assert report["globally_balanced"] is True
    assert report["aggregate_capacity_exceeded"] is False
    assert report["unbalanced_transactions"] == []
    assert report["malformed_transactions"] == []
    assert report["negative_balance_violations"] == []
    assert report["orphan_transaction_entries"] == []
    assert report["orphan_account_entries"] == []
    assert report["orphan_reversal_transactions"] == []
    assert report["invalid_entries"] == []
    assert report["out_of_range_entries"] == []


async def test_audit_detects_direct_database_tampering(
    client, session_factory
):
    account = await create_account(
        client, "Tampered", "liability", allow_negative=True
    )
    with session_factory() as db:
        db.execute(
            text(
                "INSERT INTO transactions (description, status, created_at) "
                "VALUES ('manual corruption', 'posted', CURRENT_TIMESTAMP)"
            )
        )
        transaction_id = db.execute(text("SELECT last_insert_rowid()" )).scalar_one()
        db.execute(
            text(
                "INSERT INTO entries "
                "(transaction_id, account_id, direction, amount_cents, created_at) "
                "VALUES (:transaction_id, :account_id, 'debit', 123, CURRENT_TIMESTAMP)"
            ),
            {"transaction_id": transaction_id, "account_id": account["id"]},
        )
        db.commit()

    response = await client.get("/audit/verify")
    assert response.status_code == 500
    report = response.json()
    assert report["globally_balanced"] is False
    assert transaction_id in report["unbalanced_transactions"]
    assert transaction_id in report["malformed_transactions"]


async def test_audit_detects_orphan_references(client, session_factory):
    account = await create_account(client, "Existing", "asset", allow_negative=True)
    engine = session_factory.kw["bind"]
    raw = engine.raw_connection()
    try:
        raw.execute("PRAGMA foreign_keys=OFF")
        raw.execute(
            "INSERT INTO entries "
            "(transaction_id, account_id, direction, amount_cents, created_at) "
            "VALUES (999, ?, 'debit', 10, CURRENT_TIMESTAMP), "
            "(999, ?, 'credit', 10, CURRENT_TIMESTAMP)",
            (account["id"], account["id"]),
        )
        raw.commit()
    finally:
        raw.close()

    response = await client.get("/audit/verify")
    assert response.status_code == 500
    report = response.json()
    assert report["globally_balanced"] is True
    assert len(report["orphan_transaction_entries"]) == 2


async def test_audit_detects_cumulative_capacity_tampering(client, session_factory):
    left = await create_account(client, "Large asset", "asset", allow_negative=True)
    right = await create_account(client, "Large equity", "equity", allow_negative=True)
    maximum = 2**63 - 1
    first = await client.post(
        "/transactions",
        json={
            "description": "fills capacity",
            "entries": [
                {"account_id": left["id"], "direction": "debit", "amount_cents": maximum},
                {"account_id": right["id"], "direction": "credit", "amount_cents": maximum},
            ],
        },
    )
    assert first.status_code == 201
    with session_factory() as db:
        db.execute(text(
            "INSERT INTO transactions (description, status, created_at) "
            "VALUES ('out-of-band overflow', 'posted', CURRENT_TIMESTAMP)"
        ))
        transaction_id = db.execute(text("SELECT last_insert_rowid()")).scalar_one()
        db.execute(
            text(
                "INSERT INTO entries "
                "(transaction_id, account_id, direction, amount_cents, created_at) VALUES "
                "(:transaction_id, :left_id, 'debit', :amount, CURRENT_TIMESTAMP), "
                "(:transaction_id, :right_id, 'credit', :amount, CURRENT_TIMESTAMP)"
            ),
            {"transaction_id": transaction_id, "left_id": left["id"], "right_id": right["id"], "amount": maximum},
        )
        db.commit()

    response = await client.get("/audit/verify")
    assert response.status_code == 500
    assert response.json()["aggregate_capacity_exceeded"] is True


async def test_audit_detects_orphan_reversal_reference(client, session_factory):
    engine = session_factory.kw["bind"]
    raw = engine.raw_connection()
    try:
        raw.execute("PRAGMA foreign_keys=OFF")
        raw.execute(
            "INSERT INTO transactions "
            "(description, status, reversed_transaction_id, created_at) "
            "VALUES ('orphan reversal', 'posted', 999, CURRENT_TIMESTAMP)"
        )
        transaction_id = raw.execute("SELECT last_insert_rowid()").fetchone()[0]
        raw.commit()
    finally:
        raw.close()

    response = await client.get("/audit/verify")
    assert response.status_code == 500
    assert response.json()["orphan_reversal_transactions"] == [transaction_id]


def test_run_audit_releases_snapshot(db):
    report = run_audit(db)
    assert report["globally_balanced"] is True
    assert not db.in_transaction()


async def test_audit_detects_negative_balances_from_balanced_tampering(
    client, session_factory
):
    asset = await create_account(client, "Cash", "asset")
    liability = await create_account(client, "Wallet", "liability")
    with session_factory() as db:
        db.execute(
            text(
                "INSERT INTO transactions (description, status, created_at) "
                "VALUES ('balanced but invalid', 'posted', CURRENT_TIMESTAMP)"
            )
        )
        transaction_id = db.execute(text("SELECT last_insert_rowid()")).scalar_one()
        db.execute(
            text(
                "INSERT INTO entries "
                "(transaction_id, account_id, direction, amount_cents, created_at) VALUES "
                "(:transaction_id, :asset_id, 'credit', 50, CURRENT_TIMESTAMP), "
                "(:transaction_id, :liability_id, 'debit', 50, CURRENT_TIMESTAMP)"
            ),
            {
                "transaction_id": transaction_id,
                "asset_id": asset["id"],
                "liability_id": liability["id"],
            },
        )
        db.commit()

    response = await client.get("/audit/verify")
    assert response.status_code == 500
    report = response.json()
    assert report["globally_balanced"] is True
    assert report["unbalanced_transactions"] == []
    assert report["negative_balance_violations"] == [
        {"account_id": asset["id"], "balance_cents": -50},
        {"account_id": liability["id"], "balance_cents": -50},
    ]
