from collections.abc import Generator

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.database import get_db, init_db, make_engine, sqlite_url
from app.main import create_app


@pytest.fixture
def test_engine(tmp_path) -> Generator[Engine, None, None]:
    engine = make_engine(sqlite_url(tmp_path / "test-ledger.db"))
    init_db(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def session_factory(test_engine):
    return sessionmaker(bind=test_engine, expire_on_commit=False)


@pytest.fixture
def db(session_factory) -> Generator[Session, None, None]:
    with session_factory() as session:
        yield session


@pytest.fixture
def api_app(session_factory):
    app = create_app()

    def test_db():
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = test_db
    return app


@pytest_asyncio.fixture
async def client(api_app):
    transport = httpx.ASGITransport(app=api_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as value:
        yield value
