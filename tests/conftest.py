"""Test fixtures.

Each test gets a fresh in-memory database and a seeded customer table, so tests
can be run in any order and in parallel without sharing state.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.db import get_db
from app.main import app
from app.models import Base, Customer
from app.seed import SEED_CUSTOMERS

API_KEY = get_settings().api_key
AUTH = {"x-api-key": API_KEY}


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    with TestingSessionLocal() as session:
        for row in SEED_CUSTOMERS:
            session.add(Customer(**row))
        session.commit()

    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture
def client(db_session):
    def override_get_db():
        try:
            yield db_session
            db_session.commit()
        except Exception:
            db_session.rollback()
            raise

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def session_id(client) -> str:
    response = client.post(
        "/v1/sessions", json={"external_call_id": "call-test-001"}, headers=AUTH
    )
    assert response.status_code == 201
    return response.json()["session_id"]


@pytest.fixture
def verified_session(client, session_id) -> str:
    """A session that has already cleared identity verification."""
    response = client.post(
        "/v1/tools/verify_identity",
        json={
            "session_id": session_id,
            "full_name": "Rajesh Kumar",
            "date_of_birth": "1988-04-12",
            "pan": "ABCDE1234F",
        },
        headers=AUTH,
    )
    assert response.json()["outcome"] == "ok"
    return session_id
