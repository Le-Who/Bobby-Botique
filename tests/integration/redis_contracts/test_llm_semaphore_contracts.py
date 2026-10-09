"""Real Redis capacity, leases, cancellation and local degradation (ST-07)."""

import asyncio
import contextvars
import os
import time
import uuid
from types import SimpleNamespace

import pytest
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff

from app.adapters.concurrency import GlobalLLMSemaphore
from app.errors import UserLimitExceededError

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


class RoutedRedis:
    """Two independent Redis connections selected by each request's context."""

    def __init__(self):
        self.client = contextvars.ContextVar("semaphore_contract_client")

    def __getattr__(self, name):
        return getattr(self.client.get(), name)


@pytest.fixture
async def server(monkeypatch):
    url = os.environ.get("TEST_REDIS_URL")
    if not url:
        pytest.skip("TEST_REDIS_URL must identify an explicitly isolated Redis")
    clients = [Redis.from_url(url, decode_responses=True) for _ in range(2)]
    for client in clients:
        assert await client.ping()
    key = "audit:llm-semaphore:" + uuid.uuid4().hex
    router = RoutedRedis()
    router.client.set(clients[0])
    monkeypatch.setattr("app.cache.redis_client", router)
    yield clients, router, key
    await clients[0].delete(key)
    for client in clients:
        await client.aclose()


class DelayedAdmission:
    """Delay the first write before sending it to Redis, without faking answers."""

    def __init__(self, client, blocked, release, decided):
        self.client = client
        self.blocked = blocked
        self.release = release
        self.decided = decided

    def __getattr__(self, name):
        return getattr(self.client, name)

    async def zadd(self, *args, **kwargs):
        if not kwargs.get("xx"):
            self.blocked.set()
            await self.release.wait()
        return await self.client.zadd(*args, **kwargs)

    async def zrank(self, *args, **kwargs):
        value = await self.client.zrank(*args, **kwargs)
        self.decided.set()
        return value

    async def eval(self, *args, **kwargs):
        self.blocked.set()
        await self.release.wait()
        value = await self.client.eval(*args, **kwargs)
        self.decided.set()
        return value


async def server_time(client):
    seconds, micros = await client.time()
    return seconds + micros / 1_000_000


class ObservedAdmission:
    """Record a real server capacity decision, without replacing its result."""

    def __init__(self, client):
        self.client = client
        self.decided = asyncio.Event()
        self.admitted = None

    def __getattr__(self, name):
        return getattr(self.client, name)

    async def eval(self, *args, **kwargs):
        result = await self.client.eval(*args, **kwargs)
        if not self.decided.is_set():
            self.admitted = result
            self.decided.set()
        return result


async def test_queued_admission_uses_execution_time_and_keeps_first_scope_capacity(server):
    # Sampling before an awaited send can insert an already expired holder,
    # allowing a second real client into a still active limit-one provider scope.
    clients, router, key = server
    owner = GlobalLLMSemaphore(1, timeout=0.1, redis_key=key)
    contender = GlobalLLMSemaphore(1, timeout=0.1, redis_key=key)
    # Inspect admission itself before the first renewal can hide a stale score.
    owner._renew_interval = 1
    blocked, send, decided, entered, release_owner = (asyncio.Event() for _ in range(5))
    other = ObservedAdmission(clients[1])
    active = peak = 0

    async def holder():
        nonlocal active, peak
        router.client.set(DelayedAdmission(clients[0], blocked, send, decided))
        async with owner:
            active += 1
            peak = max(peak, active)
            entered.set()
            try:
                await release_owner.wait()
            finally:
                active -= 1

    async def compete():
        nonlocal active, peak
        router.client.set(other)
        async with contender:
            active += 1
            peak = max(peak, active)
            try:
                await release_owner.wait()
            finally:
                active -= 1

    first = asyncio.create_task(holder())
    second = None
    try:
        await asyncio.wait_for(blocked.wait(), 1)
        queued_at = await server_time(clients[1])
        async with asyncio.timeout(2):
            while await server_time(clients[1]) - queued_at <= 0.15:
                await asyncio.sleep(0.005)
        send_at = await server_time(clients[1])
        send.set()
        await asyncio.wait_for(entered.wait(), 1)
        admitted_at = await server_time(clients[1])
        score = (await clients[1].zrange(key, 0, -1, withscores=True))[0][1]
        second = asyncio.create_task(compete())
        await asyncio.wait_for(other.decided.wait(), 1)
        assert other.admitted == 0, "queued admission may not start with an already expired lease"
        assert peak == active == 1
        assert send_at <= score <= admitted_at
        assert await clients[1].zcard(key) == 1
    finally:
        send.set()
        release_owner.set()
        for task in (first, second):
            if task is not None:
                task.cancel()
        await asyncio.gather(*(task for task in (first, second) if task is not None), return_exceptions=True)
    assert await clients[0].zcard(key) == 0
    assert owner._local_semaphore._value == contender._local_semaphore._value == 1


@pytest.mark.parametrize(
    "owner_skew,contender_skew",
    [(-3600, 0), (0, 3600), (3600, 0)],
    ids=["slow-owner-clock", "fast-contender-clock", "fast-owner-clock"],
)
async def test_host_clock_skew_does_not_change_server_capacity_or_lease_age(
    server, monkeypatch, owner_skew, contender_skew
):
    # A slow owner or fast contender must not prune a live holder, and a fast
    # owner must not pin capacity for an hour after its configured lease.
    clients, router, key = server
    skew = contextvars.ContextVar("semaphore_host_clock_skew", default=0)
    clock = SimpleNamespace(
        time=lambda: time.time() + skew.get(), monotonic=time.monotonic, monotonic_ns=time.monotonic_ns
    )
    monkeypatch.setattr("app.adapters.concurrency.time", clock)
    owner = GlobalLLMSemaphore(1, timeout=10, redis_key=key)
    contender = GlobalLLMSemaphore(1, timeout=10, redis_key=key)
    other = ObservedAdmission(clients[1])
    entered = asyncio.Event()

    async def compete():
        router.client.set(other)
        skew.set(contender_skew)
        async with contender:
            entered.set()
            await asyncio.Future()

    router.client.set(clients[0])
    skew.set(owner_skew)
    before = await server_time(clients[0])
    async with owner:
        after = await server_time(clients[0])
        score = await clients[0].zscore(key, owner._token.get())
        task = asyncio.create_task(compete())
        try:
            await asyncio.wait_for(other.decided.wait(), 1)
            assert other.admitted == 0, "host clocks cannot evict another server-admitted scope"
            assert not entered.is_set()
            assert before <= score <= after
            assert await clients[0].zcard(key) == 1
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert await clients[0].zcard(key) == 0


@pytest.mark.parametrize("skew", [-3600, 3600], ids=["slow-clock", "fast-clock"])
async def test_renewal_uses_server_time_despite_host_clock_skew(server, monkeypatch, skew):
    # Admission alone is insufficient: a client-timestamp renewal can expire or
    # pin an otherwise healthy server lease when a replica's wall clock differs.
    clients, router, key = server
    clock = SimpleNamespace(time=lambda: time.time() + skew, monotonic=time.monotonic, monotonic_ns=time.monotonic_ns)
    monkeypatch.setattr("app.adapters.concurrency.time", clock)
    renewed = asyncio.Event()
    observed = []

    class ObserveRenewal:
        admitted = False

        def __getattr__(self, name):
            return getattr(clients[0], name)

        async def record(self, operation, token):
            before = await server_time(clients[1])
            result = await operation
            after = await server_time(clients[1])
            observed.append((before, await clients[1].zscore(key, token), after))
            renewed.set()
            return result

        async def eval(self, *args, **kwargs):
            operation = clients[0].eval(*args, **kwargs)
            if self.admitted:
                return await self.record(operation, args[-1])
            self.admitted = True
            return await operation

        async def zadd(self, *args, **kwargs):
            operation = clients[0].zadd(*args, **kwargs)
            return await self.record(operation, next(iter(args[1])))

    owner = GlobalLLMSemaphore(1, timeout=10, redis_key=key)
    owner._renew_interval = 0.05
    router.client.set(ObserveRenewal())
    async with owner:
        await asyncio.wait_for(renewed.wait(), 1)
        before, score, after = observed[0]
        assert before <= score <= after, "renewal must record execution time in the server clock domain"
    assert await clients[0].zcard(key) == 0


async def test_delayed_older_admission_cannot_enter_while_newer_holder_owns_capacity(server):
    clients, router, key = server
    first = GlobalLLMSemaphore(1, timeout=10, redis_key=key)
    second = GlobalLLMSemaphore(1, timeout=10, redis_key=key)
    blocked, release, decided = (asyncio.Event() for _ in range(3))
    entered = asyncio.Event()
    first_release = asyncio.Event()

    async def delayed_holder():
        router.client.set(DelayedAdmission(clients[0], blocked, release, decided))
        async with first:
            entered.set()
            await first_release.wait()

    task = asyncio.create_task(delayed_holder())
    try:
        await asyncio.wait_for(blocked.wait(), 1)
        router.client.set(clients[1])
        async with second:
            assert await clients[1].zcard(key) == 1
            release.set()
            await asyncio.wait_for(decided.wait(), 1)
            assert not entered.is_set(), (
                "rank recheck cannot admit an earlier delayed token over an active newer holder"
            )
            assert await clients[1].zcard(key) == 1
        await asyncio.wait_for(entered.wait(), 2)
        first_release.set()
        await task
        assert await clients[0].zcard(key) == 0
    finally:
        release.set()
        first_release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_two_clients_contend_without_exceeding_global_capacity(server):
    clients, router, key = server
    semaphores = [GlobalLLMSemaphore(3, timeout=10, redis_key=key) for _ in range(2)]
    full = asyncio.Event()
    release = asyncio.Event()
    active = peak = completed = 0

    async def holder(index):
        nonlocal active, peak, completed
        router.client.set(clients[index % 2])
        async with semaphores[index % 2]:
            active += 1
            peak = max(peak, active)
            if active == 3:
                full.set()
            try:
                await release.wait()
                await asyncio.sleep(0)
                completed += 1
            finally:
                active -= 1

    tasks = [asyncio.create_task(holder(index)) for index in range(12)]
    try:
        await asyncio.wait_for(full.wait(), 2)
        assert active == 3
        assert await clients[0].zcard(key) == 3
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 5)
        assert peak == 3
        assert active == 0 and completed == 12
        assert await clients[0].zcard(key) == 0
    finally:
        release.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_renewal_keeps_long_holder_admitted_and_exit_awaits_renewal(server):
    clients, router, key = server
    owner = GlobalLLMSemaphore(1, timeout=1, redis_key=key)
    contender = GlobalLLMSemaphore(1, timeout=1, redis_key=key)
    owner._renew_interval = 0.02
    router.client.set(clients[0])
    async with owner:
        token = owner._token.get()
        initial = await clients[0].zscore(key, token)
        renewal = owner._renewal.get()

        async def compete():
            router.client.set(clients[1])
            async with contender:
                pytest.fail("healthy renewal must retain global capacity beyond the lease duration")

        with pytest.raises(UserLimitExceededError):
            await compete()
        latest = await clients[0].zscore(key, token)
        assert latest > initial
        assert renewal is not None and not renewal.done()
    assert renewal.done()
    assert owner._local_semaphore._value == contender._local_semaphore._value == 1
    assert await clients[0].zcard(key) == 0


async def test_expired_key_is_not_resurrected_and_old_exit_preserves_new_owner(server):
    clients, router, key = server
    renewed = asyncio.Event()

    class RenewalObserver:
        admitted = False

        def __getattr__(self, name):
            return getattr(clients[0], name)

        async def eval(self, *args, **kwargs):
            result = await clients[0].eval(*args, **kwargs)
            if self.admitted:
                renewed.set()
            self.admitted = True
            return result

    old = GlobalLLMSemaphore(1, timeout=10, redis_key=key)
    old._renew_interval = 0.01
    router.client.set(RenewalObserver())
    await old.__aenter__()
    old_renewal = old._renewal.get()
    new = GlobalLLMSemaphore(1, timeout=10, redis_key=key)
    try:
        seconds, micros = await clients[0].time()
        await clients[0].pexpireat(key, seconds * 1000 + micros // 1000 - 1)
        renewed.clear()
        await asyncio.wait_for(renewed.wait(), 1)
        assert not await clients[0].exists(key), "XX renewal may not resurrect a lost owner token"
        router.client.set(clients[1])
        async with new:
            new_token = new._token.get()
            await old.__aexit__(None, None, None)
            assert old_renewal.done()
            assert await clients[1].zrange(key, 0, -1) == [new_token]
        assert await clients[0].zcard(key) == 0
    finally:
        if old._token.get() is not None:
            await old.__aexit__(None, None, None)


@pytest.mark.parametrize("after_write", [False, True], ids=["before_admission", "after_admission"])
async def test_cancel_at_real_admission_await_releases_only_request_token(server, after_write):
    clients, router, key = server
    blocked, release = asyncio.Event(), asyncio.Event()
    foreign = "foreign-holder"
    seconds, micros = await clients[0].time()
    await clients[0].zadd(key, {foreign: seconds + micros / 1_000_000})
    sem = GlobalLLMSemaphore(2, timeout=10, redis_key=key)

    class BlockAdmission:
        def __getattr__(self, name):
            return getattr(clients[1], name)

        async def eval(self, *args, **kwargs):
            if after_write:
                result = await clients[1].eval(*args, **kwargs)
            blocked.set()
            await release.wait()
            return result if after_write else await clients[1].eval(*args, **kwargs)

    async def acquire():
        router.client.set(BlockAdmission())
        async with sem:
            pytest.fail("cancelled acquisition must not enter provider scope")

    task = asyncio.create_task(acquire())
    try:
        await asyncio.wait_for(blocked.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await clients[0].zrange(key, 0, -1) == [foreign]
        assert sem._local_semaphore._value == 2
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_unreachable_redis_has_scoped_bounded_local_fallback(server):
    clients, router, key = server
    # Owned local service's adjacent unused port fails via the real transport.
    broken = Redis(
        host="127.0.0.1", port=56480, db=15, socket_connect_timeout=0.1, socket_timeout=0.1, retry=Retry(NoBackoff(), 0)
    )
    sem = GlobalLLMSemaphore(1, timeout=1, redis_key=key)
    entered, release = asyncio.Event(), asyncio.Event()

    async def holder():
        router.client.set(broken)
        async with sem:
            entered.set()
            await release.wait()

    task = asyncio.create_task(holder())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert sem._local_semaphore._value == 0
        router.client.set(broken)
        sem._timeout = 0.01
        with pytest.raises(UserLimitExceededError):
            async with sem:
                pytest.fail("Redis outage must not bypass the process-local limit")
        assert await clients[0].zcard(key) == 0
        release.set()
        await task
        assert sem._local_semaphore._value == 1
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await broken.aclose()


async def test_expired_score_is_pruned_without_removing_live_foreign_holder(server):
    clients, router, key = server
    seconds, micros = await clients[0].time()
    now = seconds + micros / 1_000_000
    await clients[0].zadd(key, {"expired-holder": now - 20, "live-holder": now})
    sem = GlobalLLMSemaphore(2, timeout=10, redis_key=key)
    async with sem:
        token = sem._token.get()
        assert set(await clients[1].zrange(key, 0, -1)) == {"live-holder", token}
    assert await clients[1].zrange(key, 0, -1) == ["live-holder"]


async def test_cancelled_holder_awaits_inflight_renewal_cleanup_and_preserves_foreign_token(server):
    clients, router, key = server
    seconds, micros = await clients[0].time()
    await clients[0].zadd(key, {"foreign-holder": seconds + micros / 1_000_000})
    renewal_started, cleanup_started, release_cleanup = (asyncio.Event() for _ in range(3))
    renewals = []

    class BlockedRenewal:
        admitted = False

        def __getattr__(self, name):
            return getattr(clients[0], name)

        async def eval(self, *args, **kwargs):
            value = await clients[0].eval(*args, **kwargs)
            if self.admitted:
                renewals.append(asyncio.current_task())
                renewal_started.set()
                try:
                    await asyncio.Future()
                finally:
                    cleanup_started.set()
                    await release_cleanup.wait()
            self.admitted = True
            return value

    sem = GlobalLLMSemaphore(2, timeout=10, redis_key=key)
    sem._renew_interval = 1

    async def holder():
        router.client.set(BlockedRenewal())
        async with sem:
            await asyncio.Future()

    task = asyncio.create_task(holder())
    try:
        await asyncio.wait_for(renewal_started.wait(), 2)
        task.cancel()
        await asyncio.wait_for(cleanup_started.wait(), 1)
        assert not task.done()
        release_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert all(renewal.done() for renewal in renewals)
        assert sem._local_semaphore._value == 2
        assert await clients[1].zrange(key, 0, -1) == ["foreign-holder"]
    finally:
        release_cleanup.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("repeat_cancel", [False, True], ids=["cancel", "repeat_cancel"])
async def test_cancellation_during_release_awaits_own_server_removal(server, repeat_cancel):
    clients, router, key = server
    seconds, micros = await clients[0].time()
    await clients[0].zadd(key, {"foreign-holder": seconds + micros / 1_000_000})
    blocked, release = asyncio.Event(), asyncio.Event()

    class BlockedRelease:
        def __getattr__(self, name):
            return getattr(clients[0], name)

        async def zrem(self, *args, **kwargs):
            blocked.set()
            await release.wait()
            return await clients[0].zrem(*args, **kwargs)

    sem = GlobalLLMSemaphore(2, timeout=10, redis_key=key)

    async def holder():
        router.client.set(BlockedRelease())
        async with sem:
            assert await clients[0].zcard(key) == 2

    task = asyncio.create_task(holder())
    try:
        await asyncio.wait_for(blocked.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "release must retain ownership until its server removal completes"
        if repeat_cancel:
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done(), "a repeated shutdown cancellation may not abandon the protected cleanup"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await clients[1].zrange(key, 0, -1) == ["foreign-holder"]
        assert sem._local_semaphore._value == 2
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
