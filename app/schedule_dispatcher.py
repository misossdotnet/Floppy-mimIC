"""Tick partagé : chaque domaine garde ses règles et sa propre table de runs.

AGENT-31 pourra enregistrer son adaptateur ici sans réutiliser les occurrences CoFFY.
Le tick ne transporte ni prompt ni secret et libère la transaction avant Redis.
"""

from dataclasses import dataclass
from typing import Callable

from app.coffy_execution import dispatch_pending as coffy_dispatch, expire_stale as coffy_expire, reserve_due as coffy_reserve


@dataclass(frozen=True)
class ScheduleAdapter:
    name: str
    reserve: Callable
    dispatch: Callable
    expire: Callable | None = None


ADAPTERS: list[ScheduleAdapter] = [ScheduleAdapter("coffy", coffy_reserve, coffy_dispatch, coffy_expire)]


def register_adapter(adapter: ScheduleAdapter):
    if any(existing.name == adapter.name for existing in ADAPTERS):
        raise ValueError("Domaine d'ordonnancement déjà enregistré.")
    ADAPTERS.append(adapter)


async def tick(factory, *, now=None):
    summary = {}
    for adapter in ADAPTERS:
        async with factory() as db:
            if adapter.expire:
                await adapter.expire(db, now=now)
            run_ids = await adapter.reserve(db, now=now)
            await db.commit()
        await adapter.dispatch(factory, now=now)
        summary[adapter.name] = len(run_ids)
    return summary
