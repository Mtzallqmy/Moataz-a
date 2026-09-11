from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field


@dataclass(slots=True)
class _LockEntry:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


_entries: dict[int, _LockEntry] = {}
_entries_guard = asyncio.Lock()


@asynccontextmanager
async def project_mutation_lock(project_id: int) -> AsyncIterator[None]:
    """Serialize mutations for one project inside this worker.

    PostgreSQL row locks remain the cross-process authority. This lock also
    protects SQLite deployments/tests, where SELECT FOR UPDATE is ignored.
    Entries are reference-counted so projects do not leak locks forever.
    """

    key = int(project_id)
    async with _entries_guard:
        entry = _entries.setdefault(key, _LockEntry())
        entry.users += 1
    await entry.lock.acquire()
    try:
        yield
    finally:
        entry.lock.release()
        async with _entries_guard:
            entry.users -= 1
            if entry.users == 0:
                _entries.pop(key, None)
