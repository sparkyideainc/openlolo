import asyncio
import contextlib
import secrets
import time
from dataclasses import dataclass

from openlolo.domain.errors import OpenLoloError


@dataclass
class Lease:
    owner: str
    token: str
    expires: float


@dataclass
class Work:
    run: object
    future: asyncio.Future
    operation_id: str | None = None
    owner: str | None = None
    token: str | None = None


class Coordinator:
    def __init__(self, journal, device, capacity=16, lease_seconds=60):
        self.journal, self.device = journal, device
        self.queue = asyncio.Queue(capacity)
        self.lease_seconds = lease_seconds
        self.lease: Lease | None = None
        self.paused = False
        self.pending = {}
        self.current = None
        self.running = None
        self.runner = self.watcher = None
        self.release_error = None

    async def start(self):
        self.journal.recover_interrupted()
        await self.release()
        self.runner = asyncio.create_task(self._loop())
        self.watcher = asyncio.create_task(self._watch())

    async def release(self):
        try:
            await asyncio.wait_for(self.device.release_all(), 1)
            self.release_error = None
        except Exception:
            self.release_error = "RELEASE_UNCONFIRMED"

    def check(self, owner, token, allow_paused=False):
        if not self.lease or self.lease.expires <= time.monotonic():
            raise OpenLoloError("LEASE_EXPIRED")
        if self.lease.owner != owner or not secrets.compare_digest(self.lease.token, token or ""):
            raise OpenLoloError("CONTROL_NOT_OWNED", status=403)
        if self.paused and not allow_paused:
            raise OpenLoloError("PAUSED")
        return self.lease

    async def acquire(self, owner):
        if self.lease and self.lease.expires <= time.monotonic():
            await self.drop_control()
        if self.lease:
            if self.lease.owner != owner:
                raise OpenLoloError("CONTROL_BUSY")
            return self.lease_status(owner, include_token=True)
        self.lease = Lease(owner, secrets.token_urlsafe(32), time.monotonic() + self.lease_seconds)
        return self.lease_status(owner, include_token=True)

    def renew(self, owner, token):
        lease = self.check(owner, token, allow_paused=True)
        lease.expires = time.monotonic() + self.lease_seconds
        return self.lease_status(owner, include_token=True)

    def lease_status(self, owner=None, include_token=False):
        lease = self.lease
        if lease is not None and lease.expires <= time.monotonic():
            lease = None
        owned = lease is not None and lease.owner == owner
        result = {
            "active": lease is not None,
            "owned": owned,
            "remaining_seconds": max(0, lease.expires - time.monotonic()) if lease is not None else 0,
            "paused": self.paused,
            "queue_depth": self.queue.qsize(),
            "release_error": self.release_error,
        }
        if include_token and lease is not None and owned:
            result["lease_token"] = lease.token
        return result

    async def drop_control(self):
        self.lease = None
        await self.stop_input()

    async def stop_input(self):
        for work in list(self.pending.values()):
            if work is self.current:
                if self.running:
                    self.running.cancel()
            else:
                self.journal.update(work.operation_id, "CANCELLED")
                work.future.cancel()
                self.pending.pop(work.operation_id, None)
        # This channel bypasses captures and the action queue.
        await self.release()

    async def pause(self):
        self.paused = True
        await self.stop_input()
        return {"paused": True, "release_error": self.release_error}

    def resume(self, owner, token):
        self.check(owner, token, allow_paused=True)
        self.paused = False
        return {"paused": False}

    def submit(self, operation_id, owner, token, kind, payload, run):
        self.check(owner, token)
        operation, fresh = self.journal.reserve(operation_id, owner, kind, payload)
        if not fresh:
            return operation
        if self.queue.full():
            return self.journal.update(operation_id, "FAILED", {"code": "QUEUE_FULL"})
        future = asyncio.get_running_loop().create_future()
        work = Work(run, future, operation_id, owner, token)
        self.pending[operation_id] = work
        self.queue.put_nowait(work)
        return operation

    async def observe(self, run):
        if self.queue.full():
            raise OpenLoloError("QUEUE_FULL", status=429)
        future = asyncio.get_running_loop().create_future()
        self.queue.put_nowait(Work(run, future))
        return await future

    async def cancel(self, operation_id, owner):
        operation = self.journal.get(operation_id, owner)
        work = self.pending.get(operation_id)
        if work:
            if work is self.current and self.running:
                self.running.cancel()
                await self.release()
            else:
                self.journal.update(operation_id, "CANCELLED")
                work.future.cancel()
                self.pending.pop(operation_id, None)
            operation = self.journal.get(operation_id, owner)
        return operation

    async def _loop(self):
        while True:
            work = await self.queue.get()
            self.current = work
            dispatched = False
            try:
                if work.future.cancelled():
                    continue
                if work.operation_id:
                    self.check(work.owner, work.token)
                    self.journal.update(work.operation_id, "DISPATCHED")
                    dispatched = True
                self.running = asyncio.create_task(work.run())
                result = await self.running
                if work.operation_id:
                    self.journal.update(work.operation_id, "SUCCEEDED", result)
                if not work.future.done():
                    work.future.set_result(result)
            except asyncio.CancelledError:
                if work.operation_id:
                    self.journal.update(work.operation_id, "OUTCOME_UNKNOWN" if dispatched else "CANCELLED")
                if not work.future.done():
                    work.future.cancel()
                if (task := asyncio.current_task()) and task.cancelling():
                    raise
            except Exception as exc:
                error = exc.as_dict() if isinstance(exc, OpenLoloError) else {"code": "BACKEND_FAILED"}
                if work.operation_id:
                    self.journal.update(
                        work.operation_id, "OUTCOME_UNKNOWN" if dispatched else "CANCELLED", error
                    )
                    if not work.future.done():
                        work.future.set_result(None)
                elif not work.future.done():
                    work.future.set_exception(exc)
            finally:
                if work.operation_id:
                    self.pending.pop(work.operation_id, None)
                self.current = self.running = None
                self.queue.task_done()

    async def _watch(self):
        while True:
            await asyncio.sleep(0.1)
            if self.lease and self.lease.expires <= time.monotonic():
                await self.drop_control()

    async def close(self):
        await self.pause()
        for task in (self.watcher, self.runner):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        while not self.queue.empty():
            work = self.queue.get_nowait()
            work.future.cancel()
            self.queue.task_done()
