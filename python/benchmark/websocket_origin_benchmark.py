"""Repeatable native Rust WebSocket routing/Origin performance comparison.

Run the SAME script on a built upstream baseline and on the fix branch:
    uv run python python/benchmark/websocket_origin_benchmark.py --output /tmp/ws-before.json
    uv run python python/benchmark/websocket_origin_benchmark.py --output /tmp/ws-after.json --compare /tmp/ws-before.json

The test app is registered once. Measurements call the real Rust
_core.handle_test_websocket function; no network or Python WebSocket handler
is executed. Repeat on the same machine with release builds and low load.
"""
from __future__ import annotations

import argparse
import asyncio
import gc
import inspect
import json
import platform
import statistics
import time
from pathlib import Path

import django
from django.conf import settings

if not settings.configured:
    settings.configure(
        SECRET_KEY="websocket-origin-benchmark-only",
        INSTALLED_APPS=["django.contrib.auth", "django.contrib.contenttypes", "django_bolt"],
        DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
    )
django.setup()

from django_bolt import BoltAPI, WebSocket, _core  # noqa: E402
from django_bolt.testing import WebSocketTestClient  # noqa: E402


async def echo_app(scope, receive, send):
    await receive()
    await send({"type": "websocket.accept"})
    while (await receive())["type"] != "websocket.disconnect":
        pass


def custom_origin(origin, context):
    return origin == "https://allowed.example"


def measure(path: str, policy: str, count: int, repeats: int) -> dict:
    api = BoltAPI()

    @api.websocket("/native/ws")
    async def native(websocket: WebSocket):
        await websocket.accept()

    mount_kwargs = {}
    if policy == "delegate":
        mount_kwargs["websocket_origin_policy"] = "delegate"
    elif policy == "any":
        mount_kwargs["websocket_origin_policy"] = "any"
    elif policy == "custom":
        mount_kwargs["websocket_origin_policy"] = "custom"
        mount_kwargs["websocket_origin_validator"] = custom_origin
    elif policy == "allowlist":
        mount_kwargs["websocket_origin_policy"] = "allowlist"
        mount_kwargs["websocket_allowed_origins"] = ["https://allowed.example"]
    api.mount_asgi("/mounted", echo_app, **mount_kwargs)
    client = WebSocketTestClient(
        api,
        path,
        headers={"Origin": "https://allowed.example"},
        cors_allowed_origins=["https://allowed.example"],
        read_django_settings=False,
    )
    app_id = client._get_or_create_app_id()
    args = (app_id, path, [("origin", "https://allowed.example")], None)
    try:
        for _ in range(500):
            _core.handle_test_websocket(*args)
        samples = []
        gc.collect()
        was_enabled = gc.isenabled()
        gc.disable()
        try:
            for _ in range(repeats):
                start = time.perf_counter_ns()
                for _ in range(count):
                    _core.handle_test_websocket(*args)
                samples.append((time.perf_counter_ns() - start) / count)
        finally:
            if was_enabled:
                gc.enable()
        ordered = sorted(samples)
        return {
            "path": path,
            "policy": policy,
            "median_ns_per_call": round(statistics.median(samples)),
            "p95_batch_ns_per_call": round(ordered[max(0, int(0.95 * (len(ordered) - 1)))]),
            "samples_ns_per_call": [round(s) for s in samples],
        }
    finally:
        client._cleanup_app()


async def full_handshake_roundtrips(policy: str, count: int) -> dict:
    """Measure a complete in-process Rust-routed WebSocket accept/close cycle."""
    api = BoltAPI()
    kwargs = {}
    if policy == "allowlist":
        kwargs = {
            "websocket_origin_policy": "allowlist",
            "websocket_allowed_origins": ["https://allowed.example"],
        }
    elif policy == "delegate":
        kwargs = {"websocket_origin_policy": "delegate"}
    elif policy == "any":
        kwargs = {"websocket_origin_policy": "any"}
    elif policy == "custom":
        kwargs = {"websocket_origin_policy": "custom", "websocket_origin_validator": custom_origin}
    api.mount_asgi("/mounted", echo_app, **kwargs)
    client_args = {
        "headers": {"Origin": "https://allowed.example"},
        "cors_allowed_origins": ["https://allowed.example"],
        "read_django_settings": False,
    }
    samples = []
    for iteration in range(count + 15):
        start = time.perf_counter_ns()
        async with WebSocketTestClient(api, "/mounted/ws", **client_args) as ws:
            assert ws.accepted
        elapsed = time.perf_counter_ns() - start
        if iteration >= 15:
            samples.append(elapsed)
    ordered = sorted(samples)
    return {
        "policy": policy,
        "median_ns_per_connection": round(statistics.median(samples)),
        "p95_ns_per_connection": round(ordered[int(0.95 * (len(ordered) - 1))]),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--compare", type=Path)
    parser.add_argument("--count", type=int, default=3000)
    parser.add_argument("--roundtrips", type=int, default=250)
    parser.add_argument("--repeats", type=int, default=9)
    args = parser.parse_args()
    if args.count < 1 or args.repeats < 1 or args.roundtrips < 1:
        parser.error("count, repeats and roundtrips must be positive")

    rows = [
        measure("/native/ws", "native", args.count, args.repeats),
        measure("/mounted/ws", "inherit", args.count, args.repeats),
    ]
    # The old baseline's API does not have the new mount options.
    if "websocket_origin_policy" in inspect.signature(BoltAPI.mount_asgi).parameters:
        rows.extend(
            [
                measure("/mounted/ws", "allowlist", args.count, args.repeats),
                measure("/mounted/ws", "delegate", args.count, args.repeats),
                measure("/mounted/ws", "any", args.count, args.repeats),
            ]
        )
    roundtrips = [asyncio.run(full_handshake_roundtrips("inherit", args.roundtrips))]
    if "websocket_origin_policy" in inspect.signature(BoltAPI.mount_asgi).parameters:
        roundtrips.extend([
            asyncio.run(full_handshake_roundtrips("allowlist", args.roundtrips)),
            asyncio.run(full_handshake_roundtrips("delegate", args.roundtrips)),
            asyncio.run(full_handshake_roundtrips("any", args.roundtrips)),
            asyncio.run(full_handshake_roundtrips("custom", args.roundtrips)),
        ])
    result = {
        "roundtrips": roundtrips,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "count": args.count,
        "repeats": args.repeats,
        "results": rows,
    }
    print(json.dumps(result, indent=2))
    if args.output:
        args.output.write_text(json.dumps(result, indent=2) + "\n")
    if args.compare:
        baseline = json.loads(args.compare.read_text())
        base = {(row["path"], row["policy"]): row for row in baseline["results"]}
        print("\nCHANGE RELATIVE TO BASELINE:")
        for row in rows:
            key = (row["path"], row["policy"])
            if key in base:
                before = base[key]["median_ns_per_call"]
                after = row["median_ns_per_call"]
                old_p95 = base[key]["p95_batch_ns_per_call"]
                new_p95 = row["p95_batch_ns_per_call"]
                print(f"{row['policy']}: median {before} -> {after} ns/call "
                      f"({(after / before - 1) * 100:+.2f}%); "
                      f"p95 batch {old_p95} -> {new_p95} ns/call "
                      f"({(new_p95 / old_p95 - 1) * 100:+.2f}%)")
            else:
                print(f"{row['policy']}: new feature, no baseline counterpart")
        old_handshakes = {entry["policy"]: entry for entry in baseline.get("roundtrips", [])}
        for entry in roundtrips:
            old = old_handshakes.get(entry["policy"])
            if old:
                before = old["median_ns_per_connection"]
                after = entry["median_ns_per_connection"]
                old_p95 = old["p95_ns_per_connection"]
                new_p95 = entry["p95_ns_per_connection"]
                print(f"full {entry['policy']}: median {before} -> {after} ns/connection "
                      f"({(after / before - 1) * 100:+.2f}%); "
                      f"p95 {old_p95} -> {new_p95} ns/connection "
                      f"({(new_p95 / old_p95 - 1) * 100:+.2f}%)")


if __name__ == "__main__":
    main()
