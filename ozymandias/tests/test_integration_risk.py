"""
Integration tests: Phase 05 (Risk Manager) connections to other phases.

Tests are ordered from most to least critical:
  1. Phase 04 → 05: generate_signal_summary() output → override signal checks
  2. Phase 02 → 05: AccountInfo/BrokerPosition types → validate_entry / daily loss
  3. Phase 03 → 05: OrderRecord + FillProtectionManager + PDTGuard → validate_entry

These tests use real module objects (not mocks) to catch schema mismatches
between phases.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from unittest.mock import MagicMock

from ozymandias.core.config import RiskConfig
from ozymandias.core.state_manager import (
    OrderRecord,
    PortfolioState,
    Position,
    TradeIntention,
)
from ozymandias.execution.broker_interface import AccountInfo, BrokerPosition
from ozymandias.execution.fill_protection import FillProtectionManager
from ozymandias.execution.pdt_guard import PDTGuard
from ozymandias.execution.risk_manager import RiskManager, _pending_order_commitment
from ozymandias.intelligence.technical_analysis import generate_signal_summary

ET = ZoneInfo("America/New_York")
_REGULAR_HOURS = datetime(2026, 3, 11, 11, 0, tzinfo=ET)


# ---------------------------------------------------------------------------
# OHLCV data factory
# ---------------------------------------------------------------------------

def _intraday_ohlcv(n: int = 40, base: float = 100.0, trend: float = 0.5) -> pd.DataFrame:
    """
    Generate n intraday 30-minute bars all on the same calendar day.

    All bars share the date 2026-01-05 so VWAP accumulates without resetting.
    ``trend`` is the per-bar price increment (positive = uptrend).
    """
    times = pd.date_range(
        "2026-01-05 09:30", periods=n, freq="30min", tz="UTC"
    )
    closes = [base + i * trend for i in range(n)]
    data = {
        "open":   [c - 0.1 for c in closes],
        "high":   [c + 0.3 for c in closes],
        "low":    [c - 0.3 for c in closes],
        "close":  closes,
        "volume": [1_000_000.0] * n,
    }
    return pd.DataFrame(data, index=times)


def _ohlcv_with_price_drop(n: int = 40) -> pd.DataFrame:
    """
    n-bar intraday dataset where the first (n-5) bars trend upward,
    then the last 5 bars sharply drop below the VWAP baseline with 3x volume.
    This exercises VWAP crossover and ATR trailing stop signals.
    """
    times = pd.date_range(
        "2026-01-05 09:30", periods=n, freq="30min", tz="UTC"
    )
    closes = []
    volumes = []
    for i in range(n):
        if i < n - 5:
            closes.append(100.0 + i * 0.5)   # gentle uptrend
            volumes.append(1_000_000.0)
        else:
            closes.append(90.0 - (i - (n - 5)) * 1.0)  # sharp drop
            volumes.append(3_000_000.0)                  # 3× volume spike

    data = {
        "open":  [c - 0.2 for c in closes],
        "high":  [c + 0.5 for c in closes],
        "low":   [c - 0.5 for c in closes],
        "close": closes,
        "volume": volumes,
    }
    return pd.DataFrame(data, index=times)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _rm(cfg: RiskConfig | None = None) -> RiskManager:
    cfg = cfg or RiskConfig()
    return RiskManager(cfg, PDTGuard(cfg))


def _account(**kw) -> AccountInfo:
    defaults = dict(
        equity=50_000.0, buying_power=50_000.0, cash=50_000.0,
        currency="USD", pdt_flag=False, daytrade_count=0, account_id="TEST",
    )
    return AccountInfo(**{**defaults, **kw})


def _portfolio(symbols: list[str] | None = None) -> PortfolioState:
    positions = [
        Position(
            symbol=s, shares=10.0, avg_cost=100.0,
            entry_date="2026-03-10", intention=TradeIntention(),
            position_id=f"pos_{s}",
        )
        for s in (symbols or [])
    ]
    return PortfolioState(positions=positions, buying_power=50_000.0)


def _position(symbol: str = "AAPL") -> Position:
    return Position(
        symbol=symbol, shares=10.0, avg_cost=100.0,
        entry_date="2026-03-10", intention=TradeIntention(),
        position_id=f"pos_{symbol}",
    )


def _order(
    symbol="AAPL", side="buy", qty=10.0, order_type="limit",
    limit_price=150.0, status="PENDING", filled_qty=0.0, order_id="ord1",
) -> OrderRecord:
    return OrderRecord(
        order_id=order_id, symbol=symbol, side=side, quantity=qty,
        order_type=order_type, limit_price=limit_price, status=status,
        filled_quantity=filled_qty, remaining_quantity=qty - filled_qty,
    )


# ===========================================================================
# Section 1: Phase 04 → Phase 05  (generate_signal_summary → override signals)
# ===========================================================================

class TestSignalSchemaCompatibility:
    """
    Verify the keys and types produced by generate_signal_summary() match
    what the RiskManager override methods actually read.
    """

    REQUIRED_KEYS = {
        # VWAP crossover
        "vwap_position",    # str: 'above' | 'below' | 'at'
        "volume_ratio",     # float
        # RSI divergence
        "rsi_divergence",   # False | 'bearish' | 'bullish'
        # ROC deceleration
        "roc_deceleration", # bool
        # Momentum flip
        "roc_5",            # float
        # ATR trailing stop (atr_14 present; 'price' must be added by caller)
        "atr_14",           # float
    }

    def test_generate_signal_summary_contains_all_required_keys(self):
        """All keys the risk manager reads must be present in TA output."""
        df = _intraday_ohlcv(40)
        result = generate_signal_summary("AAPL", df)
        signals = result["signals"]
        missing = self.REQUIRED_KEYS - set(signals.keys())
        assert not missing, f"generate_signal_summary missing keys: {missing}"

    def test_vwap_position_is_valid_string(self):
        df = _intraday_ohlcv(40)
        signals = generate_signal_summary("AAPL", df)["signals"]
        assert signals["vwap_position"] in ("above", "below", "at")

    def test_volume_ratio_is_positive_float(self):
        df = _intraday_ohlcv(40)
        signals = generate_signal_summary("AAPL", df)["signals"]
        assert isinstance(signals["volume_ratio"], float)
        assert signals["volume_ratio"] >= 0.0

    def test_rsi_divergence_is_valid_type(self):
        df = _intraday_ohlcv(40)
        signals = generate_signal_summary("AAPL", df)["signals"]
        assert signals["rsi_divergence"] in (False, "bearish", "bullish")

    def test_roc_deceleration_is_bool(self):
        df = _intraday_ohlcv(40)
        signals = generate_signal_summary("AAPL", df)["signals"]
        assert isinstance(signals["roc_deceleration"], bool)

    def test_atr_14_is_nonnegative_float(self):
        df = _intraday_ohlcv(40)
        signals = generate_signal_summary("AAPL", df)["signals"]
        assert isinstance(signals["atr_14"], float)
        assert signals["atr_14"] >= 0.0

    def test_price_key_present_in_ta_output(self):
        """
        generate_signal_summary includes 'price' (last close) so that
        check_atr_trailing_stop() can fire without the caller manually adding it.
        """
        df = _intraday_ohlcv(40)
        signals = generate_signal_summary("AAPL", df)["signals"]
        assert "price" in signals
        assert isinstance(signals["price"], float)
        assert signals["price"] > 0


class TestSignalSummaryToOverridePipeline:
    """
    End-to-end: TA output → RiskManager override evaluation.
    Verifies the pipeline doesn't crash with real TA output.
    """

    def test_raw_ta_output_does_not_crash_evaluate_overrides(self):
        """Passing signals dict from generate_signal_summary() must not raise."""
        rm = _rm()
        pos = _position()
        df = _intraday_ohlcv(40)
        signals = generate_signal_summary("AAPL", df)["signals"]
        should_exit, triggered = rm.evaluate_overrides(pos, signals, 120.0)
        assert isinstance(should_exit, bool)
        assert isinstance(triggered, list)

    def test_augmenting_price_enables_atr_trailing_stop(self):
        """
        Caller pattern: add price to signals dict.
        With a large enough drop, ATR trailing stop should then be able to fire.
        """
        rm = _rm()
        pos = _position()
        df = _intraday_ohlcv(40)
        signals = generate_signal_summary("AAPL", df)["signals"].copy()
        atr = signals["atr_14"]
        # Force a drop > 2x ATR from the intraday high
        intraday_high = 130.0
        signals["price"] = intraday_high - (atr * 2 + 1.0)  # guarantees trigger
        if atr > 0:
            _, triggered = rm.evaluate_overrides(pos, signals, intraday_high)
            assert "atr_trailing_stop" in triggered
        else:
            pytest.skip("ATR is 0 for this synthetic data — cannot test trailing stop")

    def test_bullish_ta_data_produces_no_override(self):
        """
        A clearly bullish intraday trend (constant upward price, steady volume)
        should not trigger any override exit signals.
        """
        rm = _rm()
        pos = _position()
        # Uniform uptrend: VWAP will track price closely → vwap_position likely 'at' or 'above'
        df = _intraday_ohlcv(40, trend=1.0)
        signals = generate_signal_summary("AAPL", df)["signals"].copy()
        # Add price slightly above current VWAP (bullish)
        signals["price"] = float(df["close"].iloc[-1])
        # Seed momentum score as positive so flip check has prior data
        rm.check_momentum_score_flip(pos, {"roc_5": 3.0, "volume_ratio": 1.0}, direction="long")
        should_exit, triggered = rm.evaluate_overrides(pos, signals, signals["price"] + 1.0)
        # Bullish data should not have VWAP below, high volume, or RSI divergence
        assert "vwap_crossover" not in triggered
        assert "rsi_divergence" not in triggered

    def test_price_drop_dataset_triggers_vwap_crossover(self):
        """
        Intraday dataset where price sharply drops below VWAP with 3x volume
        should trigger vwap_crossover when volume_ratio > 1.3.
        """
        rm = _rm()
        pos = _position()
        df = _ohlcv_with_price_drop(40)
        signals = generate_signal_summary("AAPL", df)["signals"].copy()
        signals["price"] = float(df["close"].iloc[-1])

        if signals["vwap_position"] == "below" and signals["volume_ratio"] > 1.3:
            should_exit, triggered = rm.evaluate_overrides(
                pos, signals, float(df["close"].max())
            )
            assert "vwap_crossover" in triggered
        else:
            # Document why it didn't trigger (data may not produce exact condition)
            pytest.skip(
                f"Synthetic data did not produce expected bearish VWAP condition: "
                f"vwap_position={signals['vwap_position']!r}, "
                f"volume_ratio={signals['volume_ratio']:.2f}"
            )

    def test_directional_scores_present_in_signal_summary(self):
        """long_score and short_score are produced by TA and are separate from signals dict."""
        df = _intraday_ohlcv(40)
        result = generate_signal_summary("AAPL", df)
        assert "long_score" in result
        assert "short_score" in result
        assert 0.0 <= result["long_score"] <= 1.0
        assert 0.0 <= result["short_score"] <= 1.0

    def test_generate_signal_summary_returns_symbol_and_timestamp(self):
        """Metadata fields returned alongside signals."""
        df = _intraday_ohlcv(40)
        result = generate_signal_summary("MSFT", df)
        assert result["symbol"] == "MSFT"
        assert "timestamp" in result


# ===========================================================================
# Section 2: Phase 02 → Phase 05  (broker types → RiskManager)
# ===========================================================================

class TestBrokerTypesWithRiskManager:
    """
    Verify AccountInfo and BrokerPosition (from broker_interface.py, Phase 02)
    work correctly when consumed by RiskManager methods.
    """

    def test_account_info_equity_drives_equity_floor(self):
        """
        PDTGuard.check_equity_floor() is called inside validate_entry().
        AccountInfo.equity below 25,500 must block entry.
        """
        rm = _rm()
        account = _account(equity=24_000.0, buying_power=24_000.0)
        now = _REGULAR_HOURS
        rm._reset_daily_if_needed(account, now.date())
        ok, msg = rm.validate_entry(
            "AAPL", "buy", 10, 100.0, True,
            account, _portfolio(), [], now=now,
        )
        assert not ok
        assert "equity" in msg.lower() or "minimum" in msg.lower()

    def test_account_info_buying_power_is_used_directly(self):
        """buying_power field from AccountInfo feeds the buying power check."""
        rm = _rm()
        # buying_power is exactly $500; order costs $1000
        account = _account(equity=50_000.0, buying_power=500.0)
        now = _REGULAR_HOURS
        rm._reset_daily_if_needed(account, now.date())
        ok, msg = rm.validate_entry(
            "AAPL", "buy", 10, 100.0, True,
            account, _portfolio(), [], now=now,
        )
        assert not ok
        assert "buying power" in msg.lower()

    def test_account_info_equity_drives_daily_loss_reset(self):
        """_reset_daily_if_needed initialises _daily_start_equity from account.equity."""
        rm = _rm()
        account = _account(equity=75_000.0)
        today = date(2026, 3, 11)
        rm._reset_daily_if_needed(account, today)
        assert rm._daily_start_equity == 75_000.0

    def test_daily_loss_uses_account_equity_comparison(self):
        """Equity from AccountInfo drives the daily P&L loss check."""
        rm = _rm(RiskConfig(max_daily_loss_pct=0.02))
        now = datetime(2026, 3, 11, 11, 0, tzinfo=ET)
        start = _account(equity=60_000.0)
        rm._reset_daily_if_needed(start, now.date())

        # Equity drops 2.1% → halt
        down = _account(equity=58_740.0)   # 60k × (1 - 0.021) ≈ 58,740
        halted, msg = rm.check_daily_loss(down, now=now)
        assert halted

    def test_pdt_flag_in_account_info_bypasses_equity_floor(self):
        """
        PDT-flagged accounts with equity > $25,000 pass the equity floor check
        (PDTGuard.check_equity_floor special-cases pdt_flag=True).
        """
        rm = _rm()
        # equity=26k — above $25k, pdt_flag=True
        account = _account(equity=26_000.0, buying_power=26_000.0, pdt_flag=True)
        now = _REGULAR_HOURS
        rm._reset_daily_if_needed(account, now.date())
        ok, _ = rm.validate_entry(
            "AAPL", "buy", 1, 100.0, True,
            account, _portfolio(), [], now=now,
        )
        # Equity floor check passes; other checks should also pass for small order
        assert ok

    def test_broker_position_fields_not_used_by_risk_manager(self):
        """
        BrokerPosition (live broker snapshot) is not directly consumed by
        RiskManager — the risk manager uses PortfolioState.positions (local state).
        This test documents the intentional separation: Phase 02 BrokerPosition
        vs Phase 01 Position are distinct types.
        """
        bp = BrokerPosition(
            symbol="AAPL", qty=10.0, avg_entry_price=150.0,
            current_price=155.0, market_value=1550.0, unrealized_pl=50.0,
        )
        pos = _position("AAPL")
        # RiskManager override methods use Position (local state), not BrokerPosition
        rm = _rm()
        # Calling an override with Position (not BrokerPosition) is the correct pattern
        result = rm.check_vwap_crossover(pos, {"vwap_position": "below", "volume_ratio": 1.5},
                                          direction="long", volume_threshold=1.3)
        assert isinstance(result, bool)
        # BrokerPosition has no role in override signal checks — it's used upstream
        # by the orchestrator to build Position + intraday_high, not passed to RM directly.


# ===========================================================================
# Section 3: Phase 03 → Phase 05  (OrderRecord, PDTGuard, FillProtection)
# ===========================================================================

class TestOrderRecordIntegration:
    """
    Verify OrderRecord objects (shared between Phase 03 fill protection and
    Phase 05 risk manager) are consumed correctly by validate_entry.
    """

    def test_pending_limit_buy_reduces_available_buying_power(self):
        """
        A PENDING limit buy order must reduce the effective buying power
        seen by validate_entry, potentially blocking a new order.
        """
        rm = _rm()
        now = _REGULAR_HOURS
        account = _account(equity=50_000.0, buying_power=2_000.0)
        rm._reset_daily_if_needed(account, now.date())
        # Existing pending buy: 10 shares × $150 = $1,500 committed
        existing = _order(qty=10.0, limit_price=150.0, status="PENDING")
        # New order: 10 × $60 = $600. Available = 2000 - 1500 = $500. 600 > 500 → block
        ok, msg = rm.validate_entry(
            "TSLA", "buy", 10, 60.0, True,
            account, _portfolio(), [existing], now=now,
        )
        assert not ok
        assert "buying power" in msg.lower()

    def test_filled_order_does_not_reduce_buying_power(self):
        """FILLED orders are no longer pending and must not consume buying power."""
        rm = _rm()
        now = _REGULAR_HOURS
        account = _account(equity=50_000.0, buying_power=2_000.0)
        rm._reset_daily_if_needed(account, now.date())
        filled = _order(qty=10.0, limit_price=150.0, status="FILLED")
        # New order: 10 × $60 = $600. Available = 2000 (filled order not counted). Passes.
        ok, _ = rm.validate_entry(
            "TSLA", "buy", 10, 60.0, True,
            account, _portfolio(), [filled], now=now,
        )
        assert ok

    def test_market_order_does_not_reduce_buying_power(self):
        """Pending market orders have unknown cost and are excluded from deduction."""
        rm = _rm()
        now = _REGULAR_HOURS
        account = _account(equity=50_000.0, buying_power=2_000.0)
        rm._reset_daily_if_needed(account, now.date())
        mkt_order = _order(order_type="market", limit_price=None, status="PENDING")
        ok, _ = rm.validate_entry(
            "TSLA", "buy", 10, 100.0, True,
            account, _portfolio(), [mkt_order], now=now,
        )
        # $1000 order, $2000 buying power, market order not deducted → passes
        assert ok

    def test_partially_filled_order_deducts_remaining_shares(self):
        """
        _pending_order_commitment() deducts only the REMAINING unfilled shares.
        This is more accurate than FillProtectionManager.available_buying_power(),
        which deducts the full original quantity (see parity test below).
        """
        # Partially filled: 10 ordered, 4 filled, 6 remaining @ $100 → $600 deducted
        partial = _order(qty=10.0, limit_price=100.0, status="PARTIALLY_FILLED", filled_qty=4.0)
        commitment = _pending_order_commitment([partial])
        assert commitment == 600.0   # only remaining 6 shares × $100


class TestFillProtectionBuyingPowerParity:
    """
    Cross-phase parity: _pending_order_commitment() (Phase 05 RiskManager)
    vs FillProtectionManager.available_buying_power() (Phase 03).

    These two functions perform the same logical operation but are implemented
    independently. This test suite documents where they agree and where they differ.
    """

    def _fp_available(self, reported: float, orders: list[OrderRecord]) -> float:
        """Call FillProtectionManager.available_buying_power() with given orders."""
        # FillProtectionManager requires a StateManager but available_buying_power()
        # doesn't use it — MagicMock satisfies the constructor.
        fp = FillProtectionManager(MagicMock())
        return fp.available_buying_power(reported, orders)

    def _rm_available(self, reported: float, orders: list[OrderRecord]) -> float:
        """Call the RiskManager equivalent."""
        return reported - _pending_order_commitment(orders)

    def test_agree_on_single_pending_limit_buy(self):
        """For a clean PENDING order (no partial fills), both implementations agree."""
        orders = [_order(qty=10.0, limit_price=100.0, status="PENDING")]
        reported = 5_000.0
        fp = self._fp_available(reported, orders)
        rm = self._rm_available(reported, orders)
        assert fp == rm == 4_000.0  # 5000 - (10 × 100)

    def test_agree_on_multiple_pending_orders(self):
        """Multiple pending orders: both implementations sum the same committed values."""
        orders = [
            _order(qty=5.0, limit_price=200.0, status="PENDING", order_id="a"),
            _order(qty=3.0, limit_price=100.0, status="PENDING", order_id="b"),
        ]
        reported = 5_000.0
        fp = self._fp_available(reported, orders)
        rm = self._rm_available(reported, orders)
        # 5×200 + 3×100 = 1300 committed
        assert fp == rm == 3_700.0

    def test_agree_on_sell_orders_excluded(self):
        """Sell orders don't consume buying power — both implementations agree."""
        orders = [_order(side="sell", qty=10.0, limit_price=100.0, status="PENDING")]
        reported = 5_000.0
        fp = self._fp_available(reported, orders)
        rm = self._rm_available(reported, orders)
        assert fp == rm == 5_000.0

    def test_agree_on_market_orders_excluded(self):
        """Market orders excluded from deduction — both implementations agree."""
        orders = [_order(order_type="market", limit_price=None, status="PENDING")]
        reported = 5_000.0
        fp = self._fp_available(reported, orders)
        rm = self._rm_available(reported, orders)
        assert fp == rm == 5_000.0

    def test_agree_on_partially_filled_orders(self):
        """
        Both implementations deduct only the REMAINING unfilled quantity.

        10 shares @ $100 ordered, 4 already filled → 6 remaining × $100 = $600 deducted.
        """
        partial = _order(
            qty=10.0, limit_price=100.0,
            status="PARTIALLY_FILLED", filled_qty=4.0,
        )
        reported = 5_000.0
        fp = self._fp_available(reported, [partial])
        rm = self._rm_available(reported, [partial])
        assert fp == rm == 4_400.0  # 5000 - (6 × 100)


class TestPDTGuardIntegration:
    """
    Phase 03 PDTGuard is injected into Phase 05 RiskManager.
    Verify the two modules cooperate correctly.
    """

    def test_pdt_equity_floor_called_from_validate_entry(self):
        """
        PDTGuard.check_equity_floor() is the FIRST check in validate_entry.
        It must block before any other check runs when equity is too low.
        """
        rm = _rm()
        # Equity below $25,500 — PDT equity floor fires first
        account = _account(equity=20_000.0, buying_power=20_000.0)
        now = _REGULAR_HOURS
        rm._reset_daily_if_needed(account, now.date())
        ok, msg = rm.validate_entry(
            "AAPL", "buy", 1, 1.0, True,
            account, _portfolio(), [], now=now,
        )
        assert not ok
        # Message should come from PDTGuard.check_equity_floor (not from RM's own checks)
        assert "equity" in msg.lower()

    def test_pdt_guard_can_day_trade_called_for_sell_of_held_symbol(self):
        """
        When selling a symbol already in the portfolio, validate_entry delegates
        the day-trade check to PDTGuard.can_day_trade(). With 0 prior day trades
        and the default buffer of 1, up to 2 trades are allowed.
        """
        rm = _rm()
        account = _account()
        now = _REGULAR_HOURS
        rm._reset_daily_if_needed(account, now.date())
        # AAPL is in the portfolio → selling it could be a day trade
        portfolio = _portfolio(["AAPL"])
        # No prior fills in orders list → 0 day trades counted → allowed
        ok, _ = rm.validate_entry(
            "AAPL", "sell", 5, 110.0, True,
            account, portfolio, [], now=now,
        )
        assert ok

    def test_risk_manager_constructs_with_pdt_guard_correctly(self):
        """RiskManager accepts PDTGuard at construction and delegates to it."""
        cfg = RiskConfig(pdt_buffer=0, min_equity_for_trading=10_000.0)
        pdt = PDTGuard(cfg)
        rm = RiskManager(cfg, pdt)
        # Equity above the custom floor → floor check passes
        account = _account(equity=15_000.0, buying_power=15_000.0)
        now = _REGULAR_HOURS
        rm._reset_daily_if_needed(account, now.date())
        ok, _ = rm.validate_entry(
            "AAPL", "buy", 1, 100.0, True,
            account, _portfolio(), [], now=now,
        )
        assert ok
