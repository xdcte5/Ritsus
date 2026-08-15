import httpx
from sqlalchemy import inspect
from sqlalchemy.exc import OperationalError

import app.main as main_module
from app.database import get_db, make_engine, migrate_database, sqlite_url
from app.main import create_app


async def test_health_and_account_statement(client):
    health = await client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}

    readiness = await client.get("/health/ready")
    assert readiness.status_code == 200
    assert readiness.json() == {"status": "ready"}

    created = await client.post(
        "/accounts",
        json={"name": "Wallet", "type": "liability"},
    )
    assert created.status_code == 201
    assert created.json()["balance_cents"] == 0

    account_id = created.json()["id"]
    fetched = await client.get(f"/accounts/{account_id}")
    assert fetched.status_code == 200
    assert fetched.json()["type"] == "liability"
    statement = await client.get(f"/accounts/{account_id}/entries")
    assert statement.status_code == 200
    assert statement.json() == []


async def test_lifespan_applies_migrations(monkeypatch, tmp_path):
    database_url = sqlite_url(tmp_path / "lifespan.db")
    monkeypatch.setattr(
        main_module,
        "migrate_database",
        lambda: migrate_database(database_url),
    )
    application = main_module.create_app()

    async with application.router.lifespan_context(application):
        pass

    engine = make_engine(database_url)
    try:
        assert {"accounts", "transactions", "entries", "alembic_version"} <= set(
            inspect(engine).get_table_names()
        )
    finally:
        engine.dispose()


async def test_readiness_reports_database_failure():
    class FailingSession:
        def execute(self, _statement):
            raise OperationalError("SELECT 1", {}, Exception("database offline"))

        def rollback(self):
            pass

    def failing_db():
        yield FailingSession()

    app = create_app()
    app.dependency_overrides[get_db] = failing_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "error": "database_unavailable",
        "detail": "database readiness check failed",
    }


async def test_valid_and_unbalanced_transactions(client):
    cash = (
        await client.post("/accounts", json={"name": "Cash", "type": "asset"})
    ).json()
    wallet = (
        await client.post(
            "/accounts", json={"name": "Wallet", "type": "liability"}
        )
    ).json()
    payload = {
        "description": "deposit",
        "entries": [
            {"account_id": cash["id"], "direction": "debit", "amount_cents": 500},
            {"account_id": wallet["id"], "direction": "credit", "amount_cents": 500},
        ],
    }
    posted = await client.post("/transactions", json=payload)
    assert posted.status_code == 201
    assert len(posted.json()["entries"]) == 2
    fetched = await client.get(f"/transactions/{posted.json()['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["description"] == "deposit"
    assert (await client.get(f"/accounts/{cash['id']}")).json()["balance_cents"] == 500
    assert (await client.get(f"/accounts/{wallet['id']}")).json()["balance_cents"] == 500

    payload["entries"][1]["amount_cents"] = 499
    rejected = await client.post("/transactions", json=payload)
    assert rejected.status_code == 422
    assert rejected.json()["error"] == "unbalanced_transaction"


async def test_amount_boundaries_cannot_poison_balance_or_audit(client):
    left = (await client.post(
        "/accounts", json={"name": "Left", "type": "asset", "allow_negative": True}
    )).json()
    right = (await client.post(
        "/accounts", json={"name": "Right", "type": "equity", "allow_negative": True}
    )).json()
    too_large = 2**63
    rejected_value = await client.post(
        "/transactions",
        json={
            "description": "too large",
            "entries": [
                {"account_id": left["id"], "direction": "debit", "amount_cents": too_large},
                {"account_id": right["id"], "direction": "credit", "amount_cents": too_large},
            ],
        },
    )
    assert rejected_value.status_code == 422

    maximum = 2**63 - 1
    accepted = await client.post(
        "/transactions",
        json={
            "description": "at capacity",
            "entries": [
                {"account_id": left["id"], "direction": "debit", "amount_cents": maximum},
                {"account_id": right["id"], "direction": "credit", "amount_cents": maximum},
            ],
        },
    )
    assert accepted.status_code == 201
    cumulative = await client.post(
        "/transactions",
        json={
            "description": "past capacity",
            "entries": [
                {"account_id": left["id"], "direction": "debit", "amount_cents": 1},
                {"account_id": right["id"], "direction": "credit", "amount_cents": 1},
            ],
        },
    )
    assert cumulative.status_code == 422
    assert cumulative.json()["error"] == "invalid_ledger_input"
    assert (await client.get(f"/accounts/{left['id']}")).json()["balance_cents"] == maximum
    assert (await client.get("/audit/verify")).status_code == 200


async def test_missing_resources_return_404(client):
    assert (await client.get("/accounts/9999")).status_code == 404
    assert (await client.get("/accounts/9999/entries")).status_code == 404
    assert (await client.get("/transactions/9999")).status_code == 404
    assert (await client.post("/transactions/9999/reverse")).status_code == 404
