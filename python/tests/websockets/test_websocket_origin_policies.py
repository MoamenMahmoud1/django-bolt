"""Security, flexibility and precedence of global/per-endpoint WS Origin policies."""
from __future__ import annotations

import pytest
from django.test import override_settings

from django_bolt import BoltAPI, WebSocket
from django_bolt.testing import WebSocketTestClient


async def mounted(scope, receive, send):
    await receive()
    await send({"type": "websocket.accept"})
    await send({"type": "websocket.send", "text": "mounted"})


def make_api(*, native_options=None, mount_options=None):
    api = BoltAPI()

    @api.websocket("/native", **(native_options or {}))
    async def native(ws: WebSocket):
        await ws.accept()
        await ws.send_text("native")

    api.mount_asgi("/mounted", mounted, **(mount_options or {}))
    return api


async def connect(api, path, origin, *, read_settings=True):
    headers = {} if origin is None else {"Origin": origin}
    async with WebSocketTestClient(
        api, path, headers=headers, read_django_settings=read_settings
    ) as ws:
        return await ws.receive_text()


@pytest.mark.asyncio
async def test_global_allowlist_applies_to_native_and_asgi_and_can_be_overridden():
    with override_settings(
        BOLT_WEBSOCKET_ORIGIN_POLICY="allowlist",
        BOLT_WEBSOCKET_ALLOWED_ORIGINS=["https://global.example"],
    ):
        api = make_api(mount_options={
            "websocket_origin_policy": "allowlist",
            "websocket_allowed_origins": ["https://local.example"],
        })
        assert await connect(api, "/native", "https://global.example") == "native"
        assert await connect(api, "/mounted/ws", "https://local.example") == "mounted"
        for path, origin in [
            ("/native", "https://local.example"),
            ("/mounted/ws", "https://global.example"),
        ]:
            with pytest.raises(PermissionError, match="Origin not allowed"):
                await connect(api, path, origin)


@pytest.mark.asyncio
async def test_global_missing_origin_can_be_overridden_per_route():
    with override_settings(
        BOLT_WEBSOCKET_ORIGIN_POLICY="allowlist",
        BOLT_WEBSOCKET_ALLOWED_ORIGINS=["https://allowed.example"],
        BOLT_WEBSOCKET_ALLOW_MISSING_ORIGIN=False,
    ):
        api = make_api(native_options={"allow_missing_origin": True})
        assert await connect(api, "/native", None) == "native"
        with pytest.raises(PermissionError, match="Origin not allowed"):
            await connect(api, "/mounted/ws", None)


@pytest.mark.asyncio
async def test_any_is_opt_in_and_rejects_null_bad_origin_and_missing_by_default():
    api = make_api(native_options={"origin_policy": "any"})
    assert await connect(api, "/native", "https://external.example") == "native"
    for origin in (None, "null", "https://external.example/path"):
        with pytest.raises(PermissionError, match="Origin not allowed"):
            await connect(api, "/native", origin)

    api2 = make_api(native_options={"origin_policy": "any", "allow_missing_origin": True})
    assert await connect(api2, "/native", None) == "native"


@pytest.mark.asyncio
async def test_custom_sync_and_async_origin_validators_before_accept():
    seen = []

    def sync_validator(origin, context):
        seen.append((origin, context["path"]))
        return origin == "https://trusted.example" and context["path"] == "/native"

    async def async_validator(origin, context):
        return origin == "https://chat.example" and context["path"] == "/mounted/ws"

    api = make_api(
        native_options={"origin_policy": "custom", "origin_validator": sync_validator},
        mount_options={
            "websocket_origin_policy": "custom",
            "websocket_origin_validator": async_validator,
        },
    )
    assert await connect(api, "/native", "https://trusted.example") == "native"
    assert seen == [("https://trusted.example", "/native")]
    assert await connect(api, "/mounted/ws", "https://chat.example") == "mounted"
    for path, origin in [
        ("/native", "https://evil.example"),
        ("/mounted/ws", "https://evil.example"),
        ("/native", None),
    ]:
        with pytest.raises(PermissionError, match="Origin not allowed"):
            await connect(api, path, origin)


@pytest.mark.asyncio
async def test_custom_exception_and_non_bool_result_fail_closed():
    def crashes(origin, context):
        raise RuntimeError("validation failed")

    def invalid_result(origin, context):
        return "yes"

    for validator in (crashes, invalid_result):
        api = make_api(native_options={
            "origin_policy": "custom", "origin_validator": validator,
        })
        with pytest.raises(PermissionError, match="Origin not allowed"):
            await connect(api, "/native", "https://allowed.example")


@pytest.mark.asyncio
async def test_global_custom_validator_and_explicit_inherit_override():
    def validator(origin, context):
        return origin == "https://global.example"

    with override_settings(
        BOLT_WEBSOCKET_ORIGIN_POLICY="custom",
        BOLT_WEBSOCKET_ORIGIN_VALIDATOR=validator,
    ):
        api = make_api()
        assert await connect(api, "/native", "https://global.example") == "native"
        assert await connect(api, "/mounted/ws", "https://global.example") == "mounted"
        with pytest.raises(PermissionError, match="Origin not allowed"):
            await connect(api, "/native", "https://evil.example")

        # An explicit inherit choice overrides the global custom validator
        # and follows the default Bolt CORS policy (no CORS => reject).
        separate = make_api(native_options={"origin_policy": "inherit"})
        with pytest.raises(PermissionError, match="Origin not allowed"):
            await connect(separate, "/native", "https://global.example")


def test_invalid_local_policy_rejected_at_registration():
    with pytest.raises(ValueError, match="Origin policy"):
        make_api(native_options={"origin_policy": "typo"})
    with pytest.raises(ValueError, match="nonempty"):
        make_api(mount_options={"websocket_origin_policy": "allowlist"})
    with pytest.raises(ValueError, match="callable"):
        make_api(native_options={"origin_policy": "custom"})
    with pytest.raises(TypeError, match="allow_missing_origin"):
        make_api(native_options={"allow_missing_origin": "yes"})


@pytest.mark.asyncio
async def test_global_settings_not_read_when_test_client_opts_out():
    with override_settings(
        BOLT_WEBSOCKET_ORIGIN_POLICY="any",
    ):
        api = make_api()
        with pytest.raises(PermissionError, match="Origin not allowed"):
            await connect(api, "/native", "https://external.example", read_settings=False)
