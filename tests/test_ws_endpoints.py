"""HTTP-level tests for mac-http-server extensions.

These tests verify the HTTP /extensions introspection route and
the WS route mount. The actual fan-out / payload validation is
covered by tests/test_ws_channels.py, which tests the in-process
pub-sub that the WS handler uses internally.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel
from starlette.websockets import WebSocketDisconnect

from mac import extensions as ext
from mac.registry import Registry
from mac.storage import SQLiteTaskLedger
from mac.transport.http_ws import create_app


@pytest.fixture(autouse=True)
def _reset():
    ext.reset()
    yield
    ext.reset()


def _client() -> TestClient:
    return TestClient(create_app(Registry(SQLiteTaskLedger(":memory:"))))


def test_extensions_route_lists_registered_channels() -> None:
    class S(BaseModel):
        x: int

    ext.register(
        ext.Extension(
            name="t1",
            version="0.1.0",
            ws_channels=[ext.ChannelDef(name="t1.events", payload_schema=S, description="t1 desc")],
        )
    )
    with _client() as c:
        r = c.get("/extensions")
        assert r.status_code == 200
        body = r.json()
        assert body["extensions"] == [
            {"name": "t1", "version": "0.1.0", "channels": [{"name": "t1.events", "description": "t1 desc"}]}
        ]


def test_extensions_route_is_empty_when_nothing_registered() -> None:
    with _client() as c:
        r = c.get("/extensions")
        assert r.status_code == 200
        assert r.json() == {"extensions": []}


def test_ws_route_is_mounted_for_every_registered_channel() -> None:
    class S(BaseModel):
        n: int

    ext.register(
        ext.Extension(
            name="t1",
            version="0.1.0",
            ws_channels=[ext.ChannelDef(name="a", payload_schema=S), ext.ChannelDef(name="b", payload_schema=S)],
        )
    )
    app = create_app(Registry(SQLiteTaskLedger(":memory:")))
    ws_paths = {r.path for r in app.routes if r.__class__.__name__ == "APIWebSocketRoute"}
    assert "/ws/a" in ws_paths
    assert "/ws/b" in ws_paths


def test_ws_connection_receives_events_and_answers_ping() -> None:
    class Payload(BaseModel):
        value: str

    ext.register(
        ext.Extension(
            name="t1",
            version="0.1.0",
            ws_channels=[
                ext.ChannelDef(name="t1.events", payload_schema=Payload)
            ],
        )
    )

    with (
        _client() as client,
        client.websocket_connect("/ws/t1.events") as websocket,
    ):
        assert websocket.receive_json() == {
            "type": "ready",
            "channel": "t1.events",
        }
        assert ext.publish_to_channel(
            "t1.events",
            {"value": "hello"},
        ) == 1
        assert websocket.receive_json() == {
            "type": "event",
            "channel": "t1.events",
            "payload": {"value": "hello"},
        }
        websocket.send_json({"type": "ping"})
        assert websocket.receive_json() == {
            "type": "pong",
            "channel": "t1.events",
        }


def test_ws_channel_honors_http_token_configuration() -> None:
    class Payload(BaseModel):
        value: str

    ext.register(
        ext.Extension(
            name="t1",
            version="0.1.0",
            ws_channels=[
                ext.ChannelDef(name="t1.events", payload_schema=Payload)
            ],
        )
    )
    app = create_app(
        Registry(SQLiteTaskLedger(":memory:")),
        token="secret-token",
    )

    with TestClient(app) as client:
        with (
            pytest.raises(WebSocketDisconnect) as exc_info,
            client.websocket_connect("/ws/t1.events"),
        ):
            pass
        assert exc_info.value.code == 4401

        with client.websocket_connect(
            "/ws/t1.events?token=secret-token"
        ) as websocket:
            assert websocket.receive_json()["type"] == "ready"


def test_publish_to_registered_channel_reaches_subscribed_queue_via_app() -> None:
    """The WS handler delegates to the in-process pub-sub. Verify
    the end-to-end chain by: registering a channel, creating the
    app (which mounts the WS route), subscribing via the
    extensions API, and publishing. The subscription's queue
    receives the event.
    """
    class S(BaseModel):
        v: str

    ext.register(
        ext.Extension(
            name="t1",
            version="0.1.0",
            ws_channels=[ext.ChannelDef(name="t1.events", payload_schema=S)],
        )
    )
    _ = _client()  # ensure app is constructed and routes are mounted
    sub = ext.subscribe_to_channel("t1.events")
    delivered = ext.publish_to_channel("t1.events", {"v": "hello"})
    assert delivered == 1
    event = sub.try_recv()
    assert event == {"channel": "t1.events", "payload": {"v": "hello"}}
