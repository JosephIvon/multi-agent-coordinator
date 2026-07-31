"""Tests for mac.extensions — the downstream package extension API."""
from __future__ import annotations

import sqlite3

import pytest
from pydantic import BaseModel

from mac import extensions as ext


@pytest.fixture(autouse=True)
def _reset_extensions():
    ext.reset()
    yield
    ext.reset()


def test_register_and_get_round_trip() -> None:
    e = ext.Extension(name="t1", version="0.1.0")
    ext.register(e)
    assert ext.get("t1") is e
    assert ext.list_extensions() == [e]


def test_register_replaces_same_version_silently() -> None:
    ext.register(ext.Extension(name="t1", version="0.1.0"))
    ext.register(ext.Extension(name="t1", version="0.1.0"))
    assert len(ext.list_extensions()) == 1


def test_register_strict_rejects_version_drift() -> None:
    ext.register(ext.Extension(name="t1", version="0.1.0"))
    with pytest.raises(ext.ExtensionAlreadyRegisteredError):
        ext.register(ext.Extension(name="t1", version="0.2.0"))
    # Non-strict allows the replace.
    ext.register(ext.Extension(name="t1", version="0.2.0"), strict=False)
    assert ext.get("t1").version == "0.2.0"


def test_register_rejects_empty_name() -> None:
    with pytest.raises(ValueError):
        ext.register(ext.Extension(name="", version="0.1.0"))


def test_channel_definition_rejects_unsafe_route_name() -> None:
    class Payload(BaseModel):
        value: str

    with pytest.raises(ValueError, match="channel name"):
        ext.ChannelDef(name="../unsafe", payload_schema=Payload)


def test_unregister_returns_true_when_present_false_otherwise() -> None:
    ext.register(ext.Extension(name="t1", version="0.1.0"))
    assert ext.unregister("t1") is True
    assert ext.unregister("t1") is False
    assert ext.get("t1") is None


def test_apply_ddl_executes_every_extension_in_order() -> None:
    ext.register(
        ext.Extension(
            name="t1",
            version="0.1.0",
            table_ddl=["CREATE TABLE t1_a (id INTEGER)", "CREATE TABLE t1_b (id INTEGER)"],
        )
    )
    ext.register(
        ext.Extension(
            name="t2",
            version="0.1.0",
            table_ddl=["CREATE TABLE t2_a (id INTEGER)"],
        )
    )
    conn = sqlite3.connect(":memory:")
    count = ext.apply_ddl(conn)
    assert count == 3
    # All three tables exist
    names = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"t1_a", "t1_b", "t2_a"} <= names
    conn.close()


def test_apply_ddl_is_idempotent() -> None:
    ext.register(
        ext.Extension(
            name="t1",
            version="0.1.0",
            table_ddl=["CREATE TABLE IF NOT EXISTS t1_a (id INTEGER)"],
        )
    )
    conn = sqlite3.connect(":memory:")
    ext.apply_ddl(conn)
    ext.apply_ddl(conn)  # must not raise
    conn.close()


def test_call_hook_invokes_in_registration_order() -> None:
    order: list[str] = []

    def h1(*args, **kwargs):
        order.append("h1")
        return 1

    def h2(*args, **kwargs):
        order.append("h2")
        return 2

    ext.register(ext.Extension(name="t1", version="0.1.0", hooks={"ev": h1}))
    ext.register(ext.Extension(name="t2", version="0.1.0", hooks={"ev": h2}))
    results = ext.call_hook("ev", 7, flag=True)
    assert results == [1, 2]
    assert order == ["h1", "h2"]


def test_call_hook_skips_extensions_that_did_not_declare_it() -> None:
    ext.register(ext.Extension(name="t1", version="0.1.0", hooks={"ev": lambda: "a"}))
    ext.register(ext.Extension(name="t2", version="0.1.0"))  # no hooks
    assert ext.call_hook("ev") == ["a"]


def test_call_hook_swallows_exceptions_in_individual_hooks() -> None:
    ext.register(ext.Extension(name="t1", version="0.1.0", hooks={"ev": lambda: (_ for _ in ()).throw(RuntimeError("boom"))}))
    ext.register(ext.Extension(name="t2", version="0.1.0", hooks={"ev": lambda: "ok"}))
    assert ext.call_hook("ev") == ["ok"]


def test_get_channels_flattens_across_extensions() -> None:
    class S1(BaseModel):
        x: int

    class S2(BaseModel):
        y: str

    ext.register(
        ext.Extension(
            name="t1",
            version="0.1.0",
            ws_channels=[ext.ChannelDef(name="a", payload_schema=S1)],
        )
    )
    ext.register(
        ext.Extension(
            name="t2",
            version="0.1.0",
            ws_channels=[ext.ChannelDef(name="b", payload_schema=S2, description="b desc")],
        )
    )
    ch = ext.get_channels()
    assert set(ch.keys()) == {"a", "b"}
    assert ch["a"].payload_schema is S1
    assert ch["b"].description == "b desc"


def test_get_channels_warns_on_name_collision(caplog) -> None:
    class S(BaseModel):
        x: int

    ext.register(
        ext.Extension(name="t1", version="0.1.0", ws_channels=[ext.ChannelDef(name="dup", payload_schema=S)])
    )
    ext.register(
        ext.Extension(name="t2", version="0.1.0", ws_channels=[ext.ChannelDef(name="dup", payload_schema=S)])
    )
    with caplog.at_level("WARNING", logger="mac.extensions"):
        ch = ext.get_channels()
    assert set(ch.keys()) == {"dup"}
    assert any("dup" in rec.message for rec in caplog.records)


def test_unregister_closes_its_subscribers() -> None:
    ext.register(
        ext.Extension(
            name="t1",
            version="0.1.0",
            ws_channels=[ext.ChannelDef(name="a", payload_schema=type("S", (BaseModel,), {}))],
        )
    )
    sub = ext.subscribe_to_channel("a")
    assert ext.unregister("t1") is True
    # After unregister the subscription should yield None (closed sentinel).
    import asyncio
    result = asyncio.run(sub.wait())
    assert result is None


def test_reset_clears_everything() -> None:
    ext.register(ext.Extension(name="t1", version="0.1.0"))
    ext.register(ext.Extension(name="t2", version="0.1.0"))
    ext.reset()
    assert ext.list_extensions() == []
