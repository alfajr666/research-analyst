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
    monkeypatch.setattr("config.INTENT_BUS_PROPR_ENABLED", True)
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


def test_publish_propr_adapts_schema_v2_without_sizing(temp_bus_db):
    from intent_bus_publisher import publish_research_intent
    from intent_bus import IntentBus

    ok, delivery_id, err = publish_research_intent(_schema_v1_envelope(), target="propr")
    assert (ok, err) == (True, None)
    assert delivery_id == "ra:ra-env-1:propr"
    bus = IntentBus(db_path=temp_bus_db)
    try:
        delivery = bus.get_delivery(delivery_id)
        assert delivery.payload_schema_version == 2
        assert delivery.payload["target"] == "propr"
        assert delivery.payload["strategy_id"] == "impulse-ignition-v1"
        assert delivery.payload["thesis_id"] == "ra-env-1"
        assert delivery.payload["symbol"] == "ETH"
        assert delivery.payload["hints"]["entry"] == 3000.0
        assert delivery.payload["hints"]["sl"] == 3100.0
        assert delivery.payload["hints"]["primary_tp"] == 2800.0
        assert not any(k in delivery.payload for k in ("quantity", "risk_amount", "leverage"))
    finally:
        bus.close()


def test_target_delivery_ids_are_independent_and_idempotent(temp_bus_db):
    from intent_bus_publisher import publish_research_intent
    from intent_bus import IntentBus

    first = [publish_research_intent(_schema_v1_envelope(), target=t)[1] for t in ("bybit", "propr")]
    second = [publish_research_intent(_schema_v1_envelope(), target=t)[1] for t in ("bybit", "propr")]
    assert first == second == ["ra:ra-env-1:bybit", "ra:ra-env-1:propr"]
    bus = IntentBus(db_path=temp_bus_db)
    try:
        assert len(bus.list_deliveries()) == 2
    finally:
        bus.close()


def test_independent_consumers_receive_only_their_target(temp_bus_db):
    from intent_bus_publisher import publish_research_intent
    from intent_bus import IntentBus, models

    envelope = _schema_v1_envelope()
    assert publish_research_intent(envelope, target="bybit")[0]
    assert publish_research_intent(envelope, target="propr")[0]
    bus = IntentBus(db_path=temp_bus_db)
    try:
        bybit = bus.claim(target=models.TARGET_BYBIT, consumer="fake-bybit")
        propr = bus.claim(target=models.TARGET_PROPR, consumer="fake-propr")
        assert bybit.target == models.TARGET_BYBIT
        assert propr.target == models.TARGET_PROPR
        assert bybit.delivery_id != propr.delivery_id
        bus.complete(delivery_id=bybit.delivery_id, consumer="fake-bybit",
                     attempt=bybit.attempts, status=models.STATUS_REJECTED,
                     reason=models.REASON_POSITION_CAP)
        bus.complete(delivery_id=propr.delivery_id, consumer="fake-propr",
                     attempt=propr.attempts, status=models.STATUS_ACCEPTED,
                     reason=models.REASON_ACCEPTED)
        assert bus.get_delivery(bybit.delivery_id).status == models.STATUS_REJECTED
        assert bus.get_delivery(propr.delivery_id).status == models.STATUS_ACCEPTED
    finally:
        bus.close()
