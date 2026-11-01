"""App under test for the Origin check of a WebSocket handshake over real TCP.

The test sets the CORS settings in ``settings_extra``. The route accepts each
connection that passes the Origin check.
"""

from __future__ import annotations

from django_bolt import BoltAPI, WebSocket

api = BoltAPI()


@api.get("/health")
async def health():
    return {"status": "ok"}


@api.websocket("/ws/echo")
async def echo(websocket: WebSocket):
    await websocket.accept()
    await websocket.send_text("accepted")


def tenant_origin(origin, context):
    """Small sync custom validator, called once on the opening handshake."""
    return origin == "https://tenant.example" and context["path"] == "/ws/tenant"


@api.websocket("/ws/tenant", origin_policy="custom", origin_validator=tenant_origin)
async def tenant(websocket: WebSocket):
    await websocket.accept()
    await websocket.send_text("tenant")


@api.websocket("/ws/public", origin_policy="any", allow_missing_origin=True)
async def public(websocket: WebSocket):
    await websocket.accept()
    await websocket.send_text("public")
