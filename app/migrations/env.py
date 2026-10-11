"""Configuration Alembic du premier périmètre applicatif."""

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy import pool

from app.identity_models import Base
import app.llm_models  # noqa: F401 - enregistre les tables LLM dans Base.metadata
import app.budget_models  # noqa: F401 - budgets périodiques
import app.runtime_models  # noqa: F401 - gouvernance runtime et quotas
import app.business_models  # noqa: F401 - enregistre les tables métier dans Base.metadata
import app.agent_models  # noqa: F401 - enregistre les sessions d'agents dans Base.metadata
import app.memory_models  # noqa: F401 - mémoire et partages versionnés
import app.git_models  # noqa: F401 - dépôts, worktrees, revue et preuves
import app.incident_models  # noqa: F401 - incidents et notifications
import app.conversation_models  # noqa: F401 - tables qualifiées de coordination
import app.coffy_models  # noqa: F401 - définitions CoFFY


config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)

url = os.environ.get("MIMIC_DATABASE_URL", "")
if not url.startswith("postgresql+asyncpg://"):
    raise RuntimeError("MIMIC_DATABASE_URL doit cibler PostgreSQL via asyncpg")
config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
target_metadata = Base.metadata


def run_migrations_offline():
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True,
                      dialect_opts={"paramstyle": "named"}, include_schemas=True)
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata, include_schemas=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online():
    engine = async_engine_from_config(config.get_section(config.config_ini_section),
                                      prefix="sqlalchemy.", poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
