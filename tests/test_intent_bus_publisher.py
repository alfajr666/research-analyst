"""Research Analyst bus publisher wiring test (spec 3.2, 7, 16)."""

from __future__ import annotations

import os
import tempfile

import pytest


@pytest.fixture
def temp_bus_db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".sqlite3", prefix="ra_bus_")
    os.close(fd)
    os.remove(path)
    monkeypatch.setattr("config.INTENT_BUS_DB", path)
    monkeypatch.setattr("config.INTENT_BUS_BYBIT_ENABLED", True)
    monkeypatch.setattr("config.INTENT_BUS_LEGACY_INBOX_ENABLED", False)
    yield path
    for ext in ("", "-wal", "-shm"):
        p = path + ext
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass


def _schema_v1_envelope():
    return {
        "schema_version": 1,
        "delivery_id": "ra-env-1",
        "source": "research-analyst",
        "exchange_id": "bybit",
        "account_id": "hyro",
        "asset": "ETH",
        "symbol": "ETH/USDT:USDT",
        "direction": "SHORT",
        "entry_price": 3000.0,
        "stop_loss": 3100.0,
        "take_profit": 2800.0,
        "take_profit_mode": "fixed_full_close",
        "observed_at": "2026-01-01T00:00:00Z",
        "entry_valid_until": "2026-01-01T00:05:00Z",
        "metadata": {"strategy_id": "impulse-ignition-v1"},
    }


def test_publish_research_intent_writes_row(temp_bus_db):
    from intent_bus_publisher import publish_research_intent

    ok, delivery_id, err = publish_research_intent(_schema_v1_envelope(), target="bybit")
    assert ok is True
    assert err is None
    assert delivery_id == "ra:ra-env-1:bybit"

    # Verify the row exists in the shared bus with the right target/routing.
    sys_path = "/home/ubuntu/shared/intent-bus"
    import sys

    if sys_path not in sys.path:
        sys.path.insert(0, sys_path)
    from intent_bus import IntentBus, models

    bus = IntentBus(db_path=temp_bus_db)
    try:
        d = bus.get_delivery("ra:ra-env-1:bybit")
        assert d.target == models.TARGET_BYBIT
        assert d.exchange_id == "bybit"
        assert d.account_id == "hyro"
        assert d.payload["symbol"] == "ETH/USDT:USDT"
        assert "order_type" not in d.payload
    finally:
        bus.close()


def test_publish_disabled_when_flags_off(temp_bus_db, monkeypatch):
    monkeypatch.setattr("config.INTENT_BUS_BYBIT_ENABLED", False)
    from intent_bus_publisher import publish_research_intent

    ok, delivery_id, err = publish_research_intent(_schema_v1_envelope(), target="bybit")
    # Disabled -> no publish attempted, safe no-op.
    assert ok is False or delivery_id is None
