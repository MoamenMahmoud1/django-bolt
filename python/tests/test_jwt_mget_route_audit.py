"""Focused Redis integration tests: both JWT logout endpoints use one primary MGET.

Run within django-bolt's python/tests directory; requires a LOCAL TEST Redis.
Does not modify source or flush Redis; uses random prefixes and removes own keys.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from django.test import override_settings

from django_bolt.auth import DjangoCacheRevocation, JWTAuthViews, create_token_pair
from django_bolt.exceptions import HTTPException

pytestmark = pytest.mark.django_db

SECRET = "jwt-mget-endpoint-integration-test-key"


class _Routes:
    def __init__(self):
        self.handlers = {}

    def post(self, path, **_kwargs):
        def register(fn):
            self.handlers[path] = fn
            return fn

        return register


def _settings():
    return {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": os.environ.get("DJANGO_BOLT_TEST_REDIS_URL", "redis://127.0.0.1:6379/15"),
            "KEY_PREFIX": "bolt-jwt-mget-endpoint-test",
        }
    }


def _setup():
    pytest.importorskip("redis")
    store = DjangoCacheRevocation(key_prefix=f"mget-routes:{uuid.uuid4().hex}:")
    backend = store.cache._cache
    client = backend.get_client(write=True)
    try:
        client.ping()
    except Exception as exc:
        pytest.skip(f"Test Redis unavailable: {exc}")
    routes = _Routes()
    JWTAuthViews(store=store, secret=SECRET).register(routes)
    pair = create_token_pair("mget-route-user", secret=SECRET)
    request = {"context": {"auth_claims": pair.refresh_claims, "user_id": "mget-route-user"}}
    prefix = store.key_prefix
    fam_key = store.cache.make_and_validate_key(f"{prefix}fam:{pair.refresh_claims['fam']}")
    ver_key = store.cache.make_and_validate_key(f"{prefix}ver:mget-route-user")
    jti_key = store.cache.make_and_validate_key(f"{prefix}{pair.refresh_claims['jti']}")
    return backend, client, routes, request, (fam_key, ver_key, jti_key)


@pytest.mark.parametrize("endpoint", ["/auth/logout", "/auth/logout-all"])
def test_each_logout_uses_one_primary_mget_and_no_individual_get(monkeypatch, endpoint):
    with override_settings(CACHES=_settings()):
        backend, original_client, routes, request, keys = _setup()
        get_client = backend.get_client
        calls = {"mget": 0, "get": 0, "primary_mget": 0}

        def tracked_factory(*args, **kwargs):
            client = get_client(*args, **kwargs)
            real_mget = client.mget
            real_get = client.get

            def tracked_mget(*margs, **mkwargs):
                calls["mget"] += 1
                calls["primary_mget"] += kwargs.get("write") is True
                return real_mget(*margs, **mkwargs)

            def tracked_get(*gargs, **gkwargs):
                calls["get"] += 1
                return real_get(*gargs, **gkwargs)

            monkeypatch.setattr(client, "mget", tracked_mget)
            monkeypatch.setattr(client, "get", tracked_get)
            return client

        try:
            monkeypatch.setattr(backend, "get_client", tracked_factory)
            response = asyncio.run(routes.handlers[endpoint](request))
            assert response.status_code == 200
            assert calls == {"mget": 1, "get": 0, "primary_mget": 1}

            # Direct checks using the originally acquired client (unpatched),
            # without influencing the Redis request counters.
            if endpoint == "/auth/logout":
                assert original_client.get(keys[0]) is not None
                assert original_client.get(keys[1]) is None
            else:
                assert original_client.get(keys[0]) is None
                version_raw = original_client.get(keys[1])
                assert backend._serializer.loads(version_raw) == 1
        finally:
            original_client.delete(*keys)


@pytest.mark.parametrize("endpoint", ["/auth/logout", "/auth/logout-all"])
def test_mget_failure_never_performs_logout_mutation(monkeypatch, endpoint):
    with override_settings(CACHES=_settings()):
        backend, original_client, routes, request, keys = _setup()
        get_client = backend.get_client

        def failing_factory(*args, **kwargs):
            client = get_client(*args, **kwargs)
            if kwargs.get("write") is True:
                def unavailable(*_args, **_kwargs):
                    raise RuntimeError("simulated security read outage")

                monkeypatch.setattr(client, "mget", unavailable)
            return client

        try:
            monkeypatch.setattr(backend, "get_client", failing_factory)
            with pytest.raises(RuntimeError, match="simulated security read outage"):
                asyncio.run(routes.handlers[endpoint](request))
            assert original_client.get(keys[0]) is None  # Family not revoked.
            assert original_client.get(keys[1]) is None  # Version not bumped.
        finally:
            original_client.delete(*keys)


# Backward compatibility: optional MGET must not silently bypass custom
# security checks provided by DjangoCacheRevocation subclasses.
class _ExtraJTIRevocation(DjangoCacheRevocation):
    async def is_revoked(self, jti: str) -> bool:
        return True


class _ExtraFamilyRevocation(DjangoCacheRevocation):
    async def is_family_revoked(self, fam: str) -> bool:
        return True


class _ExtraVersionPolicy(DjangoCacheRevocation):
    async def get_user_version(self, user_id: str) -> int:
        return 37


@pytest.mark.parametrize("endpoint", ["/auth/logout", "/auth/logout-all"], ids=["logout", "logout-all"])
@pytest.mark.parametrize(
    "store_class",
    [_ExtraJTIRevocation, _ExtraFamilyRevocation, _ExtraVersionPolicy],
    ids=["custom-jti", "custom-family", "custom-version"],
)
def test_subclass_security_overrides_not_skipped_by_mget(store_class, endpoint):
    with override_settings(CACHES=_settings()):
        store = store_class(key_prefix=f"mget-custom:{uuid.uuid4().hex}:")
        backend = store.cache._cache
        client = backend.get_client(write=True)
        try:
            client.ping()
        except Exception as exc:
            pytest.skip(f"Test Redis unavailable: {exc}")

        routes = _Routes()
        JWTAuthViews(store=store, secret=SECRET).register(routes)
        user_id = "mget-custom-override-user"
        pair = create_token_pair(user_id, secret=SECRET)
        request = {"context": {"auth_claims": pair.refresh_claims, "user_id": user_id}}
        prefix = store.key_prefix
        fam_key = store.cache.make_and_validate_key(f"{prefix}fam:{pair.refresh_claims['fam']}")
        ver_key = store.cache.make_and_validate_key(f"{prefix}ver:{user_id}")
        jti_key = store.cache.make_and_validate_key(f"{prefix}{pair.refresh_claims['jti']}")
        try:
            with pytest.raises(HTTPException) as error:
                asyncio.run(routes.handlers[endpoint](request))
            assert error.value.status_code == 401
            assert client.get(fam_key) is None, "Invalid logout must not revoke a family"
            assert client.get(ver_key) is None, "Invalid logout-all must not bump the user version"
        finally:
            client.delete(fam_key, ver_key, jti_key)
