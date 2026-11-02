"""Custom-origin admission must not starve the normal Bolt worker pool."""
from __future__ import annotations

import asyncio
import threading

import pytest

from django_bolt.websocket import origin_policy
from django_bolt.websocket.origin_policy import OriginOptions, resolve_origin_options

CTX = {"path": "/ws", "query_string": "", "headers": {}}


def checked_for(validator):
    policy = resolve_origin_options(
        OriginOptions(policy="custom", validator=validator), read_settings=False
    )
    return policy._bolt_websocket_origin_validator


@pytest.mark.asyncio
async def test_custom_async_validation_has_bounded_admission_and_releases_on_cancel():
    waiting = asyncio.Event()

    async def held(_origin, _ctx):
        await waiting.wait()
        return True

    checked = checked_for(held)
    tasks = [asyncio.create_task(checked("https://trusted.example", CTX)) for _ in range(
        origin_policy._MAX_PENDING_CUSTOM_ORIGINS
    )]
    try:
        await asyncio.sleep(0.05)
        with pytest.raises(RuntimeError, match="Too many pending"):
            await checked("https://trusted.example", CTX)
    finally:
        waiting.set()
        assert all(await asyncio.gather(*tasks))
    assert await checked("https://trusted.example", CTX)


@pytest.mark.asyncio
async def test_slow_sync_validators_cannot_starve_default_workers_or_free_slots_on_cancel():
    unblock = threading.Event()
    started = 0
    guard = threading.Lock()

    def held(_origin, _ctx):
        nonlocal started
        with guard:
            started += 1
        unblock.wait(timeout=10)
        return True

    checked = checked_for(held)
    tasks = [asyncio.create_task(checked("https://trusted.example", CTX)) for _ in range(
        origin_policy._SYNC_ORIGIN_WORKERS
    )]
    try:
        for _ in range(300):
            with guard:
                if started == origin_policy._SYNC_ORIGIN_WORKERS:
                    break
            await asyncio.sleep(0.01)
        assert started == origin_policy._SYNC_ORIGIN_WORKERS
        with pytest.raises(RuntimeError, match="Too many synchronous"):
            await checked("https://trusted.example", CTX)
        # Explicit cancellation must NOT release a worker still inside held().
        tasks[0].cancel()
        await asyncio.gather(tasks[0], return_exceptions=True)
        with pytest.raises(RuntimeError, match="Too many synchronous"):
            await checked("https://trusted.example", CTX)
        # A shared-pool implementation would deadlock or starve this job.
        assert await asyncio.wait_for(asyncio.to_thread(lambda: "normal handler"), 2) == "normal handler"
    finally:
        unblock.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        # A thread completion callback can run just after awaiting the wrapper.
        for _ in range(200):
            if origin_policy._sync_origin_slots._value == origin_policy._SYNC_ORIGIN_WORKERS:
                break
            await asyncio.sleep(0.01)
    assert await checked("https://trusted.example", CTX)
