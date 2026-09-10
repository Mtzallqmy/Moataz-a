from __future__ import annotations

import asyncio
import itertools
from collections import Counter
from collections.abc import Awaitable, Callable

from app.config import get_settings
from app.services.render_service import render_service

settings = get_settings()
RenderRunner = Callable[[int], Awaitable[None]]
UserLookup = Callable[[int], Awaitable[int]]
RenderCanceller = Callable[[int, int | None], Awaitable[bool]]
CancelRequester = Callable[[int], bool]


class RenderQueue:
    """Independent in-process queue for Media Studio renders."""

    def __init__(
        self,
        *,
        runner: RenderRunner,
        user_lookup: UserLookup,
        global_limit: int,
        per_user_limit: int,
        canceller: RenderCanceller | None = None,
        cancel_requester: CancelRequester | None = None,
    ) -> None:
        self.runner = runner
        self.user_lookup = user_lookup
        self.global_limit = max(1, int(global_limit))
        self.per_user_limit = max(1, int(per_user_limit))
        self.canceller = canceller or (
            lambda render_job_id, user_id: render_service.cancel_render(
                render_job_id, user_id=user_id
            )
        )
        self.cancel_requester = cancel_requester or render_service.request_cancel
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
        self._dispatcher = asyncio.create_task(self._dispatch(), name="render-queue-dispatcher")

    async def enqueue(self, render_job_id: int, *, priority: int = 0) -> bool:
        if render_job_id in self._known:
            return False
        user_id = await self.user_lookup(render_job_id)
        self._known.add(render_job_id)
        await self.queue.put((-int(priority), next(self._sequence), render_job_id, user_id))
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
            _, _, render_job_id, user_id = item
            self._active_users[user_id] += 1
            task = asyncio.create_task(
                self._run_one(render_job_id, user_id),
                name=f"render-job-{render_job_id}",
            )
            self._active[render_job_id] = task
            self.queue.task_done()

    async def _run_one(self, render_job_id: int, user_id: int) -> None:
        try:
            await self.runner(render_job_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            # RenderService persists failures. Queue survival is intentional.
            pass
        finally:
            self._active.pop(render_job_id, None)
            self._known.discard(render_job_id)
            self._active_users[user_id] -= 1
            if self._active_users[user_id] <= 0:
                self._active_users.pop(user_id, None)
            async with self._condition:
                self._condition.notify_all()

    async def cancel(self, render_job_id: int, *, user_id: int | None = None) -> bool:
        return await self.canceller(render_job_id, user_id)

    async def shutdown(self) -> None:
        if not self._running:
            return
        self._running = False
        for render_job_id in list(self._active):
            self.cancel_requester(render_job_id)
        if self._dispatcher is not None:
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
        while True:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            else:
                self.queue.task_done()

    @property
    def active_render_ids(self) -> tuple[int, ...]:
        return tuple(sorted(self._active))


_manager: RenderQueue | None = None


def get_render_queue() -> RenderQueue:
    global _manager
    if _manager is None:
        _manager = RenderQueue(
            runner=render_service.process_render,
            user_lookup=render_service.get_render_user_id,
            global_limit=settings.max_concurrent_renders,
            per_user_limit=settings.max_renders_per_user,
        )
    return _manager


async def start_render_queue() -> None:
    await get_render_queue().start()


async def enqueue_render(render_job_id: int, *, priority: int = 0) -> bool:
    manager = get_render_queue()
    await manager.start()
    return await manager.enqueue(render_job_id, priority=priority)


async def cancel_render(render_job_id: int, *, user_id: int | None = None) -> bool:
    return await get_render_queue().cancel(render_job_id, user_id=user_id)


async def shutdown_render_queue() -> None:
    await get_render_queue().shutdown()
