"""Tests for the mac.extensions WebSocket channel pub-sub."""
from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel, ValidationError

from mac import extensions as ext


@pytest.fixture(autouse=True)
def _reset():
    ext.reset()
    yield
    ext.reset()


def test_subscribe_returns_a_handle() -> None:
    class Payload(BaseModel):
        x: int

    ext.register(
        ext.Extension(
            name="t1",
            version="0.1.0",
            ws_channels=[ext.ChannelDef(name="a", payload_schema=Payload)],
        )
    )
    sub = ext.subscribe_to_channel("a")
    assert isinstance(sub, ext.ChannelSubscription)
    assert sub.channel == "a"
    sub.unsubscribe()


def test_publish_to_channel_with_no_subscribers_returns_zero() -> None:
    class Payload(BaseModel):
        x: int

    ext.register(
        ext.Extension(
            name="t1",
            version="0.1.0",
            ws_channels=[ext.ChannelDef(name="a", payload_schema=Payload)],
        )
    )
    assert ext.publish_to_channel("a", {"x": 1}) == 0


def test_publish_fans_out_to_all_subscribers() -> None:
    class Payload(BaseModel):
        x: int

    ext.register(
        ext.Extension(
            name="t1",
            version="0.1.0",
            ws_channels=[ext.ChannelDef(name="a", payload_schema=Payload)],
        )
    )
    sub1 = ext.subscribe_to_channel("a")
    sub2 = ext.subscribe_to_channel("a")
    delivered = ext.publish_to_channel("a", {"x": 42})
    assert delivered == 2
    # Each subscriber should have one event queued.
    assert sub1.try_recv() == {"channel": "a", "payload": {"x": 42}}
    assert sub2.try_recv() == {"channel": "a", "payload": {"x": 42}}
    # No more events.
    assert sub1.try_recv() is None
    assert sub2.try_recv() is None


def test_publish_validates_payload_against_schema() -> None:
    class Strict(BaseModel):
        n: int

    ext.register(
        ext.Extension(
            name="t1",
            version="0.1.0",
            ws_channels=[ext.ChannelDef(name="strict", payload_schema=Strict)],
        )
    )
    sub = ext.subscribe_to_channel("strict")
    with pytest.raises(ValidationError):
        ext.publish_to_channel("strict", {"n": "not-an-int"})
    # Nothing was published.
    assert sub.try_recv() is None
    # Valid payload goes through.
    delivered = ext.publish_to_channel("strict", {"n": 1})
    assert delivered == 1
    assert sub.try_recv() == {"channel": "strict", "payload": {"n": 1}}


def test_publish_skips_subscribers_with_full_queue() -> None:
    class Payload(BaseModel):
        x: int

    ext.register(
        ext.Extension(
            name="t1",
            version="0.1.0",
            ws_channels=[ext.ChannelDef(name="a", payload_schema=Payload)],
        )
    )
    ext.subscribe_to_channel("a", maxsize=1)
    assert ext.publish_to_channel("a", {"x": 1}) == 1
    delivered = ext.publish_to_channel("a", {"x": 2})
    # Full queue: dropped, not delivered.
    assert delivered == 0


def test_unsubscribe_wakes_pending_wait_with_none() -> None:
    class Payload(BaseModel):
        x: int

    ext.register(
        ext.Extension(
            name="t1",
            version="0.1.0",
            ws_channels=[ext.ChannelDef(name="a", payload_schema=Payload)],
        )
    )
    sub = ext.subscribe_to_channel("a")

    async def run() -> None:
        sub.unsubscribe()
        return await sub.wait()

    result = asyncio.run(run())
    assert result is None


def test_unregister_extension_closes_its_channel_subscribers() -> None:
    ext.register(
        ext.Extension(
            name="t1",
            version="0.1.0",
            ws_channels=[ext.ChannelDef(name="a", payload_schema=type("S", (BaseModel,), {}))],
        )
    )
    sub = ext.subscribe_to_channel("a")
    ext.unregister("t1")

    async def run() -> None:
        return await sub.wait()

    assert asyncio.run(run()) is None


def test_unknown_channel_is_a_configuration_error() -> None:
    with pytest.raises(KeyError, match="not registered"):
        ext.subscribe_to_channel("missing")
    with pytest.raises(KeyError, match="not registered"):
        ext.publish_to_channel("missing", {"x": 1})
