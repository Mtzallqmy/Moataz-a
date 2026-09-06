from __future__ import annotations

import asyncio
import itertools
from collections import Counter
from collections.abc import Awaitable, Callable

from app.config import get_settings
from app.services.job_service import get_job_priority, get_job_user_id

settings = get_settings()
Runner = Callable[[int], Awaitable[None]]
UserLookup = Callable[[int], Awaitable[int]]


class InlineJobManager:
    def __init__(
        self,
        *,
        runner: Runner,
        user_lookup: UserLookup,
        global_limit: int,
        per_user_limit: int,
    ) -> None:
        self.runner = runner
        self.user_lookup = user_lookup
        self.global_limit = max(1, global_limit)
        self.per_user_limit = max(1, per_user_limit)
        self.queue: asyncio.PriorityQueue[tuple[int, int, int, int]] = asyncio.PriorityQueue()
        self._sequence = itertools.count()
        self._known: set[int] = set()
        self._active: dict[int, asyncio.Task] = {}
        self._active_users: Counter[int] = Counter()
        self._dispatcher: asyncio.Task | None = None
        self._running = False
        self._condition = asyncio.Condition()

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._dispatcher = asyncio.create_task(self._dispatch(), name="inline-job-dispatcher")

    async def enqueue(self, job_id: int, *, priority: int = 0) -> bool:
        if job_id in self._known:
            return False
        user_id = await self.user_lookup(job_id)
        self._known.add(job_id)
        await self.queue.put((-int(priority), next(self._sequence), job_id, user_id))
        async with self._condition:
            self._condition.notify_all()
        return True

    async def _dispatch(self) -> None:
        while self._running:
            async with self._condition:
                while self._running and len(self._active) >= self.global_limit:
                    await self._condition.wait()
            if not self._running:
                break

            item = await self.queue.get()
            deferred: list[tuple[int, int, int, int]] = []
            while self._running and self._active_users[item[3]] >= self.per_user_limit:
                deferred.append(item)
                self.queue.task_done()
                try:
                    item = self.queue.get_nowait()
                except asyncio.QueueEmpty:
                    for waiting in deferred:
                        self.queue.put_nowait(waiting)
                    deferred.clear()
                    async with self._condition:
                        await self._condition.wait()
                    item = None
                    break

            if item is None:
                continue
            if not self._running:
                self.queue.task_done()
                break

            for waiting in deferred:
                self.queue.put_nowait(waiting)

            _, _, job_id, user_id = item
            self._active_users[user_id] += 1
            task = asyncio.create_task(self._run_one(job_id, user_id), name=f"download-job-{job_id}")
            self._active[job_id] = task
            self.queue.task_done()

    async def _run_one(self, job_id: int, user_id: int) -> None:
        try:
            await self.runner(job_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The worker persists the failure. Consuming here prevents background-task exception leaks.
            pass
        finally:
            self._active.pop(job_id, None)
            self._known.discard(job_id)
            self._active_users[user_id] -= 1
            if self._active_users[user_id] <= 0:
                self._active_users.pop(user_id, None)
            async with self._condition:
                self._condition.notify_all()

    async def cancel(self, job_id: int) -> bool:
        if job_id not in self._known:
            return False
        from app.worker import request_cancel

        request_cancel(job_id)
        return True

    async def shutdown(self) -> None:
        if not self._running:
            return
        self._running = False
        from app.worker import request_cancel

        for job_id in list(self._active):
            request_cancel(job_id)
        if self._dispatcher:
            self._dispatcher.cancel()
            await asyncio.gather(self._dispatcher, return_exceptions=True)
        if self._active:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*list(self._active.values()), return_exceptions=True),
                    timeout=10,
                )
            except TimeoutError:
                for task in list(self._active.values()):
                    task.cancel()
                await asyncio.gather(*list(self._active.values()), return_exceptions=True)
        self._active.clear()
        self._known.clear()
        self._active_users.clear()

    @property
    def active_job_ids(self) -> tuple[int, ...]:
        return tuple(sorted(self._active))


_manager: InlineJobManager | None = None


async def _default_runner(job_id: int) -> None:
    from app.worker import process_download

    await process_download(job_id)


def get_job_manager() -> InlineJobManager:
    global _manager
    if _manager is None:
        _manager = InlineJobManager(
            runner=_default_runner,
            user_lookup=get_job_user_id,
            global_limit=settings.max_concurrent_jobs,
            per_user_limit=settings.max_jobs_per_user,
        )
    return _manager


async def start_queue() -> None:
    await get_job_manager().start()


async def enqueue_download(job_id: int, *, priority: int | None = None) -> None:
    manager = get_job_manager()
    await manager.start()
    effective_priority = await get_job_priority(job_id) if priority is None else int(priority)
    await manager.enqueue(job_id, priority=effective_priority)


async def cancel_download(job_id: int) -> bool:
    return await get_job_manager().cancel(job_id)


async def shutdown_queue() -> None:
    await get_job_manager().shutdown()
