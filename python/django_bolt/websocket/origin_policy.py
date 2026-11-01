"""WebSocket Origin policy configuration.

Resolve settings and per-endpoint overrides at registration, not on handshakes.
Python validators are opt-in and run only before the opening handshake.
"""
from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from ipaddress import IPv6Address
from typing import Any, Literal, TypedDict
from urllib.parse import urlsplit

from django.conf import settings
from django.utils.module_loading import import_string

NativeOriginPolicy = Literal["inherit", "allowlist", "any", "custom"]
MountOriginPolicy = Literal["inherit", "allowlist", "any", "custom", "delegate"]


class OriginContext(TypedDict):
    """Handshake request data. Client headers do not prove identity."""

    path: str
    query_string: str
    headers: dict[str, str]


OriginValidator = Callable[[str | None, OriginContext], bool | Awaitable[bool]]


@dataclass(frozen=True)
class OriginOptions:
    policy: MountOriginPolicy | None = None
    allowed_origins: list[str] | None = None
    allow_missing_origin: bool | None = None
    validator: OriginValidator | str | None = None


class ResolvedOrigin:
    """Registration wire format read exactly once by the Rust core."""

    def __init__(self, policy: str, origins: list[str], missing: bool, validator: OriginValidator | None):
        self._bolt_websocket_origin_policy = policy
        self._bolt_websocket_allowed_origins = origins
        self._bolt_websocket_allow_missing_origin = missing
        self._bolt_websocket_origin_validator = validator


def _valid_origin(origin: str) -> bool:
    if not origin or not origin.isascii() or any(ord(c) <= 32 or ord(c) == 127 for c in origin):
        return False
    try:
        parsed = urlsplit(origin)
        host = parsed.hostname
        if not host or parsed.netloc.endswith(":"):
            return False
        if ":" in host:
            IPv6Address(host)
            if "%" in host:
                return False
        elif any(c not in "abcdefghijklmnopqrstuvwxyz0123456789.-_" for c in host):
            return False
        return (
            parsed.scheme in ("http", "https")
            and parsed.username is None
            and parsed.password is None
            and parsed.path == ""
            and parsed.query == ""
            and parsed.fragment == ""
            and parsed.port != 0
            and origin == f"{parsed.scheme}://{parsed.netloc}"
        )
    except ValueError:
        return False


def _settings_value(name: str, fallback: Any, enabled: bool) -> Any:
    if not enabled or not settings.configured:
        return fallback
    return getattr(settings, name, fallback)


def resolve_origin_options(
    options: OriginOptions | None = None,
    *,
    read_settings: bool = True,
    asgi: bool = False,
) -> ResolvedOrigin:
    """Explicit endpoint values > Django settings > backward-compatible defaults."""
    local = options or OriginOptions()
    global_policy = _settings_value("BOLT_WEBSOCKET_ORIGIN_POLICY", "inherit", read_settings)
    policy = local.policy if local.policy is not None else global_policy
    if local.policy is None and policy == "delegate":
        raise ValueError("delegate must be selected explicitly on an ASGI mount")
    valid = ("inherit", "allowlist", "any", "custom", "delegate") if asgi else ("inherit", "allowlist", "any", "custom")
    if policy not in valid:
        raise ValueError(f"WebSocket Origin policy must be one of {valid}, got {policy!r}")

    global_origins = _settings_value("BOLT_WEBSOCKET_ALLOWED_ORIGINS", None, read_settings)
    origins = local.allowed_origins if local.allowed_origins is not None else global_origins
    if policy == "allowlist":
        if not isinstance(origins, (list, tuple)) or not origins:
            raise ValueError("WebSocket allowed_origins must be a nonempty list for 'allowlist'")
        for origin in origins:
            if not isinstance(origin, str) or not _valid_origin(origin):
                raise ValueError(f"Invalid WebSocket origin: {origin!r}")
    elif local.allowed_origins is not None:
        raise ValueError("allowed_origins requires Origin policy='allowlist'")

    global_missing = _settings_value("BOLT_WEBSOCKET_ALLOW_MISSING_ORIGIN", None, read_settings)
    missing = local.allow_missing_origin if local.allow_missing_origin is not None else global_missing
    if missing is None:
        missing = policy in ("inherit", "delegate")
    if type(missing) is not bool:
        raise TypeError("allow_missing_origin must be a bool or None")

    global_validator = _settings_value("BOLT_WEBSOCKET_ORIGIN_VALIDATOR", None, read_settings)
    validator = local.validator if local.validator is not None else global_validator
    if policy == "custom":
        if isinstance(validator, str):
            validator = import_string(validator)
        if not callable(validator):
            raise ValueError("Origin policy='custom' requires a callable origin_validator")
        func = validator

        async def checked(origin: str | None, context: OriginContext) -> bool:
            if inspect.iscoroutinefunction(func):
                result = await func(origin, context)
            else:
                # A slow synchronous validator must not block the worker's
                # asyncio loop. Only custom policies pay this thread cost.
                result = await asyncio.to_thread(func, origin, context)
                if inspect.isawaitable(result):
                    result = await result
            if type(result) is not bool:
                raise TypeError("WebSocket origin_validator must return bool")
            return result

        validator = checked
    elif local.validator is not None:
        raise ValueError("origin_validator requires Origin policy='custom'")
    else:
        validator = None

    return ResolvedOrigin(policy, list(origins or ()) if policy == "allowlist" else [], missing, validator)
