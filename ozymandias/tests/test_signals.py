"""
Unit tests for Phase 22 — Signal File API + Bot Event Emitter.

Tests cover:
- Signal writer functions (status, last_trade, last_review, alerts)
- Atomic write behavior (no partial writes on error)
- Signal reader (valid, missing, malformed)
- Inbound signals (check, consume, absent)
- Directory setup (ensure_signal_dirs idempotent)
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from ozymandias.core.signals import (
    SIGNALS_DIR,
    STATE_DIR,
    _atomic_write_json,
    check_inbound_signal,
    consume_inbound_signal,
    ensure_signal_dirs,
    read_signal,
    write_alert,
    write_last_review,
    write_last_trade,
    write_status,
)


@pytest.fixture(autouse=True)
def _isolated_signals(tmp_path, monkeypatch):
    """Redirect SIGNALS_DIR and STATE_DIR to a temp directory for every test."""
    signals_dir = tmp_path / "state" / "signals"
    state_dir = tmp_path / "state"
    signals_dir.mkdir(parents=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("ozymandias.core.signals.SIGNALS_DIR", signals_dir)
    monkeypatch.setattr("ozymandias.core.signals.STATE_DIR", state_dir)
    return signals_dir, state_dir


# ---------------------------------------------------------------------------
# Signal writer tests
# ---------------------------------------------------------------------------


class TestWriteStatus:
    def test_creates_valid_json(self, _isolated_signals):
        signals_dir, _ = _isolated_signals
        write_status(
            equity=100_000.0,
            positions=[{"symbol": "NVDA", "shares": 10, "avg_cost": 120.0}],
            open_orders=[],
            loop_health={"broker_available": True},
        )
        path = signals_dir / "status.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["type"] == "status"
        assert data["equity"] == 100_000.0
        assert data["position_count"] == 1
        assert data["open_order_count"] == 0
        assert "ts" in data
        assert data["loop_health"]["broker_available"] is True

    def test_overwrites_previous(self, _isolated_signals):
        signals_dir, _ = _isolated_signals
        write_status(equity=50_000.0, positions=[], open_orders=[], loop_health={})
        write_status(equity=75_000.0, positions=[], open_orders=[], loop_health={})
        data = json.loads((signals_dir / "status.json").read_text())
        assert data["equity"] == 75_000.0


class TestWriteLastTrade:
    def test_schema(self, _isolated_signals):
        signals_dir, _ = _isolated_signals
        write_last_trade(
            symbol="AAPL",
            action="entry",
            shares=50.0,
            price=175.25,
            order_id="ord-123",
            context={"side": "buy"},
        )
        data = json.loads((signals_dir / "last_trade.json").read_text())
        assert data["type"] == "last_trade"
        assert data["symbol"] == "AAPL"
        assert data["action"] == "entry"
        assert data["shares"] == 50.0
        assert data["price"] == 175.25
        assert data["order_id"] == "ord-123"
        assert data["context"]["side"] == "buy"
        assert "ts" in data


class TestWriteLastReview:
    def test_schema(self, _isolated_signals):
        signals_dir, _ = _isolated_signals
        write_last_review(
            symbol="TSLA",
            action="hold",
            reasoning_summary="Thesis intact, momentum building",
            context={"review_count": 3},
        )
        data = json.loads((signals_dir / "last_review.json").read_text())
        assert data["type"] == "last_review"
        assert data["symbol"] == "TSLA"
        assert data["action"] == "hold"
        assert data["reasoning_summary"] == "Thesis intact, momentum building"
        assert data["context"]["review_count"] == 3
        assert "ts" in data


class TestWriteAlert:
    def test_creates_unique_files(self, _isolated_signals):
        signals_dir, _ = _isolated_signals
        alerts_dir = signals_dir / "alerts"
        alerts_dir.mkdir(parents=True, exist_ok=True)

        write_alert("equity_drawdown", "WARNING", "Equity down 2.5%")
        # Ensure different timestamp in filename
        time.sleep(0.01)
        write_alert("loop_stall", "ERROR", "Loop tick exceeded 60s")

        alert_files = list(alerts_dir.glob("*.json"))
        assert len(alert_files) == 2

    def test_append_only(self, _isolated_signals):
        signals_dir, _ = _isolated_signals
        alerts_dir = signals_dir / "alerts"
        alerts_dir.mkdir(parents=True, exist_ok=True)

        write_alert("broker_error", "WARNING", "First error")
        first_files = set(alerts_dir.glob("*.json"))

        time.sleep(0.01)
        write_alert("broker_error", "WARNING", "Second error")
        all_files = set(alerts_dir.glob("*.json"))

        # First alert file still exists, plus a new one
        assert first_files.issubset(all_files)
        assert len(all_files) == 2


class TestAtomicWrite:
    def test_survives_crash(self, _isolated_signals):
        """Verify no partial writes — temp file cleaned up on error."""
        signals_dir, _ = _isolated_signals
        target = signals_dir / "crash_test.json"

        class BrokenEncoder:
            def __str__(self):
                raise RuntimeError("simulated crash")

        with pytest.raises(RuntimeError):
            _atomic_write_json(target, {"bad": BrokenEncoder()})

        # Target should not exist (write failed)
        assert not target.exists()
        # No leftover .tmp files
        tmp_files = list(signals_dir.glob("*.tmp"))
        assert len(tmp_files) == 0


# ---------------------------------------------------------------------------
# Signal reader tests
# ---------------------------------------------------------------------------


class TestReadSignal:
    def test_valid(self, _isolated_signals):
        signals_dir, _ = _isolated_signals
        write_status(equity=42_000.0, positions=[], open_orders=[], loop_health={})
        data = read_signal(signals_dir / "status.json")
        assert data is not None
        assert data["equity"] == 42_000.0

    def test_missing(self, _isolated_signals):
        signals_dir, _ = _isolated_signals
        result = read_signal(signals_dir / "nonexistent.json")
        assert result is None

    def test_malformed(self, _isolated_signals):
        signals_dir, _ = _isolated_signals
        bad_path = signals_dir / "bad.json"
        bad_path.write_text("not valid json {{{")
        result = read_signal(bad_path)
        assert result is None


# ---------------------------------------------------------------------------
# Inbound signal tests
# ---------------------------------------------------------------------------


class TestCheckInboundSignal:
    def test_present(self, _isolated_signals):
        _, state_dir = _isolated_signals
        (state_dir / "PAUSE_ENTRIES").touch()
        assert check_inbound_signal("PAUSE_ENTRIES") is True

    def test_absent(self, _isolated_signals):
        assert check_inbound_signal("PAUSE_ENTRIES") is False


class TestConsumeInboundSignal:
    def test_consume(self, _isolated_signals):
        _, state_dir = _isolated_signals
        (state_dir / "FORCE_REASONING").touch()
        assert consume_inbound_signal("FORCE_REASONING") is True
        # File should be deleted after consumption
        assert not (state_dir / "FORCE_REASONING").exists()

    def test_absent(self, _isolated_signals):
        assert consume_inbound_signal("FORCE_REASONING") is False


# ---------------------------------------------------------------------------
# Directory setup tests
# ---------------------------------------------------------------------------


class TestEnsureSignalDirs:
    def test_creates_all(self, _isolated_signals):
        signals_dir, state_dir = _isolated_signals
        ensure_signal_dirs()
        expected = [
            signals_dir,
            signals_dir / "alerts",
            signals_dir / "orchestrator",
            signals_dir / "conductor",
            signals_dir / "architect",
            signals_dir / "reviewer",
            signals_dir / "analyst",
            signals_dir / "dialogue",
        ]
        for d in expected:
            assert d.is_dir(), f"Missing directory: {d}"
        assert (state_dir / "agent_tasks").is_dir()

    def test_idempotent(self, _isolated_signals):
        ensure_signal_dirs()
        ensure_signal_dirs()  # second call should not raise
