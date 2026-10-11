"""Tests PostgreSQL opt-in, limités à une base locale dédiée mimic_test."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.identity_models import Base, User
import app.agent_models  # noqa: F401 - tables techniques dans le nettoyage des tests
import app.coffy_models  # noqa: F401 - définitions CoFFY dans le nettoyage des tests
from app.identity_service import database_factory, password_hasher, seed_roles
from app.agent_service import seed_skill_profiles
from app.main import app


ROOT = Path(__file__).resolve().parents[1]
ADMIN_PASSWORD = "Mot-de-passe-administrateur-2026!"


async def with_db(callback):
    factory = database_factory()
    try:
        async with factory() as db:
            result = await callback(db)
            await db.commit()
            return result
    finally:
        await factory.kw["bind"].dispose()


@pytest.fixture
def pg_client(monkeypatch):
    url = os.environ.get("MIMIC_TEST_DATABASE_URL", "")
    parsed = urlsplit(url)
    if not url:
        pytest.skip("MIMIC_TEST_DATABASE_URL non définie")
    if parsed.scheme != "postgresql+asyncpg" or parsed.hostname not in {"localhost", "127.0.0.1"} or parsed.path != "/mimic_test":
        pytest.fail("Les tests d'intégration exigent une base locale nommée mimic_test.")
    monkeypatch.setenv("MIMIC_DATABASE_URL", url)
    monkeypatch.setenv("MIMIC_AUTH_KEY", "cle-de-test-longue-et-ephemere-de-48-caracteres-minimum")
    monkeypatch.setenv("MIMIC_COOKIE_SECURE", "false")
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, check=True)

    async def reset(db):
        names = ", ".join((f'"{table.schema}".' if table.schema else '') + f'"{table.name}"'
                          for table in Base.metadata.sorted_tables)
        await db.execute(text(f"TRUNCATE TABLE {names} RESTART IDENTITY CASCADE"))
        await seed_roles(db)
        await seed_skill_profiles(db)

    asyncio.run(with_db(reset))
    with TestClient(app, follow_redirects=False) as client:
        yield client


def create_user(*, admin=False) -> tuple[str, str]:
    email = f"test-{uuid4().hex[:12]}@example.org"

    async def save(db):
        user = User(email=email, display_name="Compte de test", is_active=True,
                    is_platform_admin=admin, password_hash=password_hasher.hash(ADMIN_PASSWORD))
        db.add(user)
        await db.flush()
        return user.id

    return asyncio.run(with_db(save)), email
