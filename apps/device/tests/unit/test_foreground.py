"""Foreground observer: an answer counts only under the sequence and epoch it started with."""

import asyncio

import pytest

from openlolo.config import Config
from openlolo.coredevice.foreground import ForegroundObserver, classify
from openlolo.domain.errors import OpenLoloError


def proc(bundle, foreground=True, application=True):
    return {"bundleIdentifier": bundle, "foregroundRunning": foreground, "isApplication": application}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeSource:
    """Answers queries from ``answers``; ``hook`` runs while a query is in flight."""

    def __init__(self, answers, clock, cost=0.4, hook=None):
        self.answers, self.clock, self.cost, self.hook = list(answers), clock, cost, hook
        self.queries = 0
        self.closed = False
        self.events_queue: asyncio.Queue = asyncio.Queue()

    async def open(self):
        pass

    async def processes(self):
        self.queries += 1
        self.clock.now += self.cost
        if self.hook is not None:
            self.hook(self.queries)
        answer = self.answers[min(self.queries, len(self.answers)) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def events(self):
        while True:
            yield await self.events_queue.get()

    async def close(self):
        self.closed = True


def observer(source, clock, **overrides):
    config = Config(foreground_observer="demand", **overrides)
    return ForegroundObserver(lambda: source, config, clock)


SAFARI = [proc("com.apple.springboard"), proc("com.apple.InputUI"), proc("com.apple.mobilesafari")]


def test_classify_contexts():
    def ctx(processes):
        return classify(processes, 0, 0, 0, "e").context

    assert ctx(SAFARI) == "app"
    assert ctx([proc("com.apple.springboard"), proc("com.apple.InputUI")]) == "system"
    assert ctx([proc("com.apple.springboard"), proc("a.b"), proc("c.d")]) == "ambiguous"
    assert ctx([proc("com.apple.springboard"), proc(None)]) == "ambiguous"
    # Background apps and non-application processes never count.
    assert ctx([proc("a.b", foreground=False), proc(None, application=False)]) == "system"


async def test_answer_is_reused_until_max_age_from_query_start():
    clock = Clock()
    source = FakeSource([SAFARI], clock)
    fg = observer(source, clock, foreground_max_age=1.5)
    first = await fg.snapshot()
    assert first["context"] == "app" and first["queried"] and first["apps"] == ["com.apple.mobilesafari"]
    clock.now += 1.0  # 1.4 s after the query started.
    assert not (await fg.snapshot())["queried"]
    clock.now += 0.2  # 1.6 s: expired.
    assert (await fg.snapshot())["queried"]
    assert source.queries == 2
    await fg.close()


async def test_invalidation_forces_a_new_query():
    clock = Clock()
    source = FakeSource([SAFARI], clock)
    fg = observer(source, clock)
    await fg.snapshot()
    fg.invalidate("input")
    assert (await fg.snapshot())["queried"]
    assert source.queries == 2
    await fg.close()


async def test_query_overtaken_by_an_invalidation_does_not_count():
    clock = Clock()
    holder: list[ForegroundObserver] = []
    source = FakeSource([SAFARI], clock, hook=lambda n: n == 1 and holder[0].invalidate("notification"))
    fg = observer(source, clock)
    holder.append(fg)
    result = await fg.snapshot()
    assert source.queries == 2 and result["queried"]
    assert fg.status()["superseded"] == 1
    await fg.close()


async def test_constant_invalidation_fails_closed():
    clock = Clock()
    holder: list[ForegroundObserver] = []
    source = FakeSource([SAFARI], clock, hook=lambda n: holder[0].invalidate("notification"))
    fg = observer(source, clock)
    holder.append(fg)
    with pytest.raises(OpenLoloError) as error:
        await fg.snapshot()
    assert error.value.code == "FOREGROUND_UNVERIFIED"
    await fg.close()


async def test_failed_query_closes_the_source_and_reports_unavailable():
    clock = Clock()
    source = FakeSource([RuntimeError("dtx closed"), SAFARI], clock)
    fg = observer(source, clock)
    with pytest.raises(OpenLoloError) as error:
        await fg.snapshot()
    assert error.value.code == "FOREGROUND_UNAVAILABLE"
    assert source.closed and fg.source is None
    assert (await fg.snapshot())["context"] == "app"  # Reopens on the next request.
    assert fg.status()["opens"] == 2
    await fg.close()


async def test_app_state_events_invalidate():
    clock = Clock()
    source = FakeSource([SAFARI], clock)
    fg = observer(source, clock)
    await fg.snapshot()
    event = {"appName": "YouTube", "pid": 1, "state_description": "Running"}
    await source.events_queue.put(("applicationStateNotification:", [event]))
    for _ in range(5):
        await asyncio.sleep(0)
    assert not fg.status()["valid"]
    assert fg.status()["events"][-1][1:] == ["YouTube", "Running"]
    await fg.close()


async def test_concurrent_requests_share_one_query():
    clock = Clock()
    source = FakeSource([SAFARI], clock)
    fg = observer(source, clock)
    await asyncio.gather(fg.snapshot(), fg.snapshot(), fg.snapshot())
    assert source.queries == 1
    await fg.close()


async def test_idle_closes_the_connection():
    clock = Clock()
    source = FakeSource([SAFARI], clock)
    fg = observer(source, clock, foreground_idle_seconds=5)
    await fg.snapshot()
    assert fg.source is not None
    clock.now += 6
    fg.idle_task.cancel()
    await fg._idle()
    assert fg.source is None and source.closed
    assert not fg.status()["valid"]
    await fg.close()
