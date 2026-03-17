"""
Tests for execution/risk_manager.py.

All external dependencies (broker, PDT guard, market hours) are mocked or
controlled via dependency injection. No real broker calls, no real clock.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
import pytest

from ozymandias.core.config import RiskConfig, SchedulerConfig
from ozymandias.core.state_manager import (
    OrderRecord,
    PortfolioState,
    Position,
    TradeIntention,
)
from ozymandias.execution.broker_interface import AccountInfo
from ozymandias.execution.pdt_guard import PDTGuard
from ozymandias.execution.risk_manager import RiskManager, _pending_order_commitment

ET = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _cfg(**overrides) -> RiskConfig:
    """Build a RiskConfig with optional overrides."""
    cfg = RiskConfig()
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _pdt(cfg: RiskConfig | None = None) -> PDTGuard:
    return PDTGuard(cfg or _cfg())


def _rm(cfg: RiskConfig | None = None) -> RiskManager:
    c = cfg or _cfg()
    return RiskManager(c, _pdt(c))


def _account(
    equity: float = 50_000.0,
    buying_power: float = 50_000.0,
    pdt_flag: bool = False,
    daytrade_count: int = 0,
) -> AccountInfo:
    return AccountInfo(
        equity=equity,
        buying_power=buying_power,
        cash=equity,
        currency="USD",
        pdt_flag=pdt_flag,
        daytrade_count=daytrade_count,
        account_id="TEST",
    )


def _portfolio(symbols: list[str] | None = None) -> PortfolioState:
    positions = []
    for sym in (symbols or []):
        positions.append(
            Position(
                symbol=sym,
                shares=10.0,
                avg_cost=100.0,
                entry_date="2026-03-10",
                intention=TradeIntention(),
                position_id=f"pos_{sym}",
            )
        )
    return PortfolioState(positions=positions, buying_power=50_000.0)


def _order(
    symbol: str = "AAPL",
    side: str = "buy",
    qty: float = 10.0,
    order_type: str = "limit",
    limit_price: float | None = 150.0,
    status: str = "PENDING",
    filled_qty: float = 0.0,
    order_id: str = "ord1",
) -> OrderRecord:
    return OrderRecord(
        order_id=order_id,
        symbol=symbol,
        side=side,
        quantity=qty,
        order_type=order_type,
        limit_price=limit_price,
        status=status,
        filled_quantity=filled_qty,
        remaining_quantity=qty - filled_qty,
    )


# Regular hours: Wednesday 2026-03-11 11:00 AM ET
_REGULAR_HOURS = datetime(2026, 3, 11, 11, 0, tzinfo=ET)
# Last 5 minutes: Wednesday 2026-03-11 3:57 PM ET
_LAST_5_MIN = datetime(2026, 3, 11, 15, 57, tzinfo=ET)
# Pre-market: Wednesday 2026-03-11 8:00 AM ET
_PRE_MARKET = datetime(2026, 3, 11, 8, 0, tzinfo=ET)
# Post-market: Wednesday 2026-03-11 5:00 PM ET
_POST_MARKET = datetime(2026, 3, 11, 17, 0, tzinfo=ET)


def _position(symbol: str = "AAPL", entry_date: str = "2026-03-10") -> Position:
    return Position(
        symbol=symbol,
        shares=10.0,
        avg_cost=150.0,
        entry_date=entry_date,
        intention=TradeIntention(),
        position_id=f"pos_{symbol}",
    )


# ---------------------------------------------------------------------------
# Position size calculation
# ---------------------------------------------------------------------------

class TestCalculatePositionSize:
    def test_basic_formula(self):
        """shares = (acct * risk_pct) / (atr * mult)"""
        rm = _rm()
        # (100_000 * 0.01) / (5.0 * 2.0) = 100
        assert rm.calculate_position_size("AAPL", 200.0, 5.0, 100_000.0) == 100

    def test_capped_by_max_position_pct(self):
        """Cap: (100_000 * 0.20) / 200.0 = 100 shares."""
        rm = _rm(_cfg(max_position_pct=0.20))
        # raw = (100_000 * 0.01) / (1.0 * 2.0) = 500 → capped at 100
        assert rm.calculate_position_size("AAPL", 200.0, 1.0, 100_000.0) == 100

    def test_zero_atr_returns_zero(self):
        assert _rm().calculate_position_size("AAPL", 100.0, 0.0, 50_000.0) == 0

    def test_zero_price_returns_zero(self):
        assert _rm().calculate_position_size("AAPL", 0.0, 2.0, 50_000.0) == 0

    def test_zero_account_returns_zero(self):
        assert _rm().calculate_position_size("AAPL", 100.0, 2.0, 0.0) == 0

    def test_truncates_to_integer(self):
        # (50_000 * 0.01) / (3.0 * 2.0) = 83.33… → 83
        result = _rm().calculate_position_size("AAPL", 100.0, 3.0, 50_000.0)
        assert result == 83

    def test_custom_risk_and_multiplier(self):
        # (100_000 * 0.02) / (5.0 * 3.0) = 133.33 → 133
        # Cap: (100_000 * 0.20) / 50.0 = 400 — does not bind
        result = _rm().calculate_position_size(
            "AAPL", 50.0, 5.0, 100_000.0,
            risk_per_trade_pct=0.02, atr_multiplier=3.0,
        )
        assert result == 133


# ---------------------------------------------------------------------------
# validate_entry — individual checks
# ---------------------------------------------------------------------------

class TestValidateEntry:

    def _call(self, rm=None, symbol="AAPL", side="buy", qty=10, price=100.0,
              blocks_eod_entries=True, account=None, portfolio=None, orders=None,
              avg_daily_volume=None, now=None):
        rm = rm or _rm()
        account = account or _account()
        portfolio = portfolio or _portfolio()
        orders = orders or []
        now = now or _REGULAR_HOURS
        # Seed daily tracker so halt check doesn't fail on first call
        rm._reset_daily_if_needed(account, now.date())
        return rm.validate_entry(
            symbol, side, qty, price, blocks_eod_entries,
            account, portfolio, orders,
            avg_daily_volume=avg_daily_volume, now=now,
        )

    def test_passes_all_checks(self):
        ok, msg = self._call()
        assert ok
        assert "passed" in msg

    # Equity floor
    def test_equity_floor_blocks_below_25500(self):
        ok, msg = self._call(account=_account(equity=25_000.0, buying_power=25_000.0))
        assert not ok
        assert "below minimum" in msg.lower() or "equity" in msg.lower()

    # Position size
    def test_position_size_exceeded(self):
        # 100 shares × $200 = $20k > 20% of $50k equity ($10k)
        ok, msg = self._call(qty=100, price=200.0)
        assert not ok
        assert "exceed" in msg.lower() or "position size" in msg.lower()

    def test_position_size_at_limit_passes(self):
        # Exactly 20%: 10 shares × $100 = $1000 / $50k equity = 2% — passes
        ok, msg = self._call(qty=10, price=100.0)
        assert ok

    # Concurrent positions
    def test_max_concurrent_positions_blocks(self):
        symbols = [f"S{i}" for i in range(8)]  # 8 = max_concurrent_positions default
        ok, msg = self._call(portfolio=_portfolio(symbols))
        assert not ok
        assert "concurrent" in msg.lower()

    def test_seven_positions_allows_entry(self):
        symbols = [f"S{i}" for i in range(7)]
        ok, _ = self._call(portfolio=_portfolio(symbols))
        assert ok

    # Market hours
    def test_pre_market_blocks_entry(self):
        ok, msg = self._call(now=_PRE_MARKET)
        assert not ok
        assert "regular hours" in msg.lower()

    def test_post_market_blocks_entry(self):
        ok, msg = self._call(now=_POST_MARKET)
        assert not ok
        assert "regular hours" in msg.lower()

    def test_blocks_eod_entries_last_5_min_blocked(self):
        ok, msg = self._call(blocks_eod_entries=True, now=_LAST_5_MIN)
        assert not ok
        assert "5 minutes" in msg or "Entry blocked" in msg

    def test_no_eod_block_last_5_min_allowed(self):
        ok, _ = self._call(blocks_eod_entries=False, now=_LAST_5_MIN)
        assert ok

    # Buying power
    def test_insufficient_buying_power_blocks(self):
        # Account has $1000 buying power, order costs $2000
        ok, msg = self._call(
            account=_account(equity=50_000.0, buying_power=1_000.0),
            qty=20, price=200.0,  # $4000 > $1000
        )
        assert not ok
        assert "buying power" in msg.lower()

    def test_pending_orders_reduce_buying_power(self):
        # Pending limit buy: 10 shares @ $150 = $1500 committed
        # Account BP: $2000. Remaining: $500. New order: 10 × $100 = $1000 > $500.
        pending = _order(qty=10.0, limit_price=150.0, status="PENDING")
        ok, msg = self._call(
            account=_account(equity=50_000.0, buying_power=2_000.0),
            orders=[pending],
            qty=10, price=100.0,  # $1000 > $500 available
        )
        assert not ok
        assert "buying power" in msg.lower()

    def test_sell_side_skips_buying_power_check(self):
        # Selling doesn't require buying power
        ok, _ = self._call(
            side="sell",
            account=_account(equity=50_000.0, buying_power=0.0),
            portfolio=_portfolio(["AAPL"]),  # hold the symbol
            now=_REGULAR_HOURS,
        )
        # May fail PDT but NOT buying power
        # (PDT only triggers if it's actually a day trade, which requires
        # the order history to show a same-day buy fill — here orders=[] so no day trades)
        assert ok

    # Minimum volume
    def test_min_volume_blocks_low_volume_stock(self):
        ok, msg = self._call(avg_daily_volume=50_000.0)
        assert not ok
        assert "volume" in msg.lower()

    def test_min_volume_passes_adequate_volume(self):
        ok, _ = self._call(avg_daily_volume=500_000.0)
        assert ok

    def test_none_avg_volume_skips_check(self):
        ok, _ = self._call(avg_daily_volume=None)
        assert ok


# ---------------------------------------------------------------------------
# Daily loss halt
# ---------------------------------------------------------------------------

class TestDailyLossHalt:
    def test_halt_triggers_at_threshold(self):
        rm = _rm(_cfg(max_daily_loss_pct=0.02))
        today = date(2026, 3, 11)
        now = datetime(2026, 3, 11, 11, 0, tzinfo=ET)
        # Seed start equity
        start_account = _account(equity=50_000.0)
        rm._reset_daily_if_needed(start_account, today)
        # Drop equity by 2.1% — exceeds limit
        down_account = _account(equity=48_950.0)  # -2.1%
        halted, msg = rm.check_daily_loss(down_account, now=now)
        assert halted
        assert "limit" in msg.lower()

    def test_no_halt_within_threshold(self):
        rm = _rm(_cfg(max_daily_loss_pct=0.02))
        now = datetime(2026, 3, 11, 11, 0, tzinfo=ET)
        start = _account(equity=50_000.0)
        rm._reset_daily_if_needed(start, now.date())
        small_loss = _account(equity=49_500.0)  # -1%
        halted, _ = rm.check_daily_loss(small_loss, now=now)
        assert not halted

    def test_halt_persists_same_day(self):
        rm = _rm(_cfg(max_daily_loss_pct=0.02))
        today = date(2026, 3, 11)
        now = datetime(2026, 3, 11, 11, 0, tzinfo=ET)
        start = _account(equity=50_000.0)
        rm._reset_daily_if_needed(start, today)
        # Trigger halt
        down = _account(equity=48_900.0)  # -2.2%
        rm.check_daily_loss(down, now=now)
        # Even if equity recovers, still halted today
        recover = _account(equity=50_000.0)
        halted, msg = rm.check_daily_loss(recover, now=now)
        assert halted

    def test_halt_resets_next_day(self):
        rm = _rm(_cfg(max_daily_loss_pct=0.02))
        # Day 1: trigger halt
        d1 = datetime(2026, 3, 11, 11, 0, tzinfo=ET)
        start = _account(equity=50_000.0)
        rm._reset_daily_if_needed(start, d1.date())
        rm.check_daily_loss(_account(equity=48_900.0), now=d1)
        # Day 2: reset
        d2 = datetime(2026, 3, 12, 11, 0, tzinfo=ET)
        fresh = _account(equity=48_900.0)
        halted, _ = rm.check_daily_loss(fresh, now=d2)
        assert not halted

    def test_validate_entry_blocks_when_halted(self):
        rm = _rm(_cfg(max_daily_loss_pct=0.02))
        now = datetime(2026, 3, 11, 11, 0, tzinfo=ET)
        start = _account(equity=50_000.0)
        rm._reset_daily_if_needed(start, now.date())
        # Trigger halt
        down = _account(equity=48_900.0)
        rm.check_daily_loss(down, now=now)
        # validate_entry should now block
        ok, msg = rm.validate_entry(
            "AAPL", "buy", 10, 100.0, True,
            down, _portfolio(), [], now=now,
        )
        assert not ok
        assert "halt" in msg.lower()


# ---------------------------------------------------------------------------
# Override signals
# ---------------------------------------------------------------------------

class TestVwapCrossover:
    def test_triggers_below_vwap_high_volume(self):
        rm = _rm()
        pos = _position()
        indicators = {"vwap_position": "below", "volume_ratio": 1.5}
        assert rm.check_vwap_crossover(pos, indicators) is True

    def test_no_trigger_below_vwap_low_volume(self):
        rm = _rm()
        pos = _position()
        indicators = {"vwap_position": "below", "volume_ratio": 1.1}
        assert rm.check_vwap_crossover(pos, indicators) is False

    def test_no_trigger_above_vwap_high_volume(self):
        rm = _rm()
        pos = _position()
        indicators = {"vwap_position": "above", "volume_ratio": 2.0}
        assert rm.check_vwap_crossover(pos, indicators) is False

    def test_no_trigger_at_vwap(self):
        rm = _rm()
        pos = _position()
        indicators = {"vwap_position": "at", "volume_ratio": 2.0}
        assert rm.check_vwap_crossover(pos, indicators) is False

    def test_boundary_volume_ratio_not_triggered(self):
        # Exactly 1.3 is NOT > 1.3, so no trigger
        rm = _rm()
        pos = _position()
        indicators = {"vwap_position": "below", "volume_ratio": 1.3}
        assert rm.check_vwap_crossover(pos, indicators) is False


class TestRsiDivergence:
    def test_bearish_divergence_detected(self):
        rm = _rm()
        pos = _position()
        assert rm.check_rsi_divergence(pos, {"rsi_divergence": "bearish"}) is True

    def test_no_divergence(self):
        rm = _rm()
        pos = _position()
        assert rm.check_rsi_divergence(pos, {"rsi_divergence": False}) is False

    def test_bullish_divergence_not_an_exit_signal(self):
        rm = _rm()
        pos = _position()
        assert rm.check_rsi_divergence(pos, {"rsi_divergence": "bullish"}) is False


class TestRocDeceleration:
    def test_triggered_when_flag_true(self):
        rm = _rm()
        pos = _position()
        assert rm.check_roc_deceleration(pos, {"roc_deceleration": True}) is True

    def test_not_triggered_when_flag_false(self):
        rm = _rm()
        pos = _position()
        assert rm.check_roc_deceleration(pos, {"roc_deceleration": False}) is False

    def test_not_triggered_when_missing(self):
        rm = _rm()
        pos = _position()
        assert rm.check_roc_deceleration(pos, {}) is False


class TestMomentumScoreFlip:
    def test_returns_false_on_first_call(self):
        rm = _rm()
        pos = _position()
        # No prior history — should not trigger
        assert rm.check_momentum_score_flip(pos, {"roc_5": 5.0, "volume_ratio": 2.0}) is False

    def test_positive_to_negative_flip_triggers(self):
        rm = _rm()
        pos = _position()
        # First call: score = 5.0 * 1.5 = 7.5 (strongly positive)
        rm.check_momentum_score_flip(pos, {"roc_5": 5.0, "volume_ratio": 1.5})
        # Second call: score = -1.0 * 1.0 = -1.0 (negative)
        assert rm.check_momentum_score_flip(pos, {"roc_5": -1.0, "volume_ratio": 1.0}) is True

    def test_weakly_positive_to_negative_no_trigger(self):
        rm = _rm()
        pos = _position()
        # First call: score = 0.5 * 1.0 = 0.5 (NOT strongly positive)
        rm.check_momentum_score_flip(pos, {"roc_5": 0.5, "volume_ratio": 1.0})
        # Second call goes negative — but prior wasn't strong, so no trigger
        assert rm.check_momentum_score_flip(pos, {"roc_5": -1.0, "volume_ratio": 1.0}) is False

    def test_negative_to_positive_flip_triggers(self):
        rm = _rm()
        pos = _position()
        # First call: score = -3.0 * 1.5 = -4.5 (strongly negative)
        rm.check_momentum_score_flip(pos, {"roc_5": -3.0, "volume_ratio": 1.5})
        # Second call positive
        assert rm.check_momentum_score_flip(pos, {"roc_5": 1.0, "volume_ratio": 1.0}) is True

    def test_per_symbol_state_independent(self):
        rm = _rm()
        pos_a = _position("AAPL")
        pos_b = _position("MSFT")
        # Seed AAPL strongly positive
        rm.check_momentum_score_flip(pos_a, {"roc_5": 5.0, "volume_ratio": 2.0})
        # MSFT has no prior — should not trigger even if score is negative
        assert rm.check_momentum_score_flip(pos_b, {"roc_5": -1.0, "volume_ratio": 1.0}) is False


class TestAtrTrailingStop:
    def test_triggers_when_drop_exceeds_2x_atr(self):
        rm = _rm()
        pos = _position()
        # high=100, price=90, atr=4. drop=10 > 2*4=8 → trigger
        assert rm.check_atr_trailing_stop(pos, {"price": 90.0, "atr_14": 4.0}, 100.0) is True

    def test_no_trigger_when_drop_below_2x_atr(self):
        rm = _rm()
        pos = _position()
        # high=100, price=94, atr=4. drop=6 < 8 → no trigger
        assert rm.check_atr_trailing_stop(pos, {"price": 94.0, "atr_14": 4.0}, 100.0) is False

    def test_exactly_2x_atr_no_trigger(self):
        rm = _rm()
        pos = _position()
        # drop == 2*atr → NOT strictly greater than → no trigger
        assert rm.check_atr_trailing_stop(pos, {"price": 92.0, "atr_14": 4.0}, 100.0) is False

    def test_missing_price_returns_false(self):
        rm = _rm()
        pos = _position()
        assert rm.check_atr_trailing_stop(pos, {"atr_14": 4.0}, 100.0) is False

    def test_zero_atr_returns_false(self):
        rm = _rm()
        pos = _position()
        assert rm.check_atr_trailing_stop(pos, {"price": 90.0, "atr_14": 0.0}, 100.0) is False


# ---------------------------------------------------------------------------
# evaluate_overrides — combined trigger logic
# ---------------------------------------------------------------------------

class TestEvaluateOverrides:
    def test_no_signals_no_exit(self):
        rm = _rm()
        pos = _position()
        indicators = {
            "vwap_position": "above",
            "volume_ratio": 0.9,
            "rsi_divergence": False,
            "roc_deceleration": False,
            "roc_5": 2.0,
            "price": 105.0,
            "atr_14": 4.0,
        }
        should_exit, signals = rm.evaluate_overrides(pos, indicators, 110.0)
        assert not should_exit
        assert signals == []

    def test_vwap_crossover_alone_triggers(self):
        rm = _rm()
        pos = _position()
        indicators = {
            "vwap_position": "below",
            "volume_ratio": 1.5,
            "rsi_divergence": False,
            "roc_deceleration": False,
            "roc_5": -1.0,
            "price": 105.0,
            "atr_14": 4.0,
        }
        # Seed momentum score so flip doesn't trigger (first call)
        should_exit, signals = rm.evaluate_overrides(pos, indicators, 108.0)
        assert should_exit
        assert "vwap_crossover" in signals

    def test_rsi_divergence_alone_does_not_trigger(self):
        rm = _rm()
        pos = _position()
        indicators = {
            "vwap_position": "above",
            "volume_ratio": 0.9,
            "rsi_divergence": "bearish",
            "roc_deceleration": False,
            "roc_5": 1.0,
            "price": 105.0,
            "atr_14": 4.0,
        }
        should_exit, signals = rm.evaluate_overrides(pos, indicators, 108.0)
        assert not should_exit
        assert "rsi_divergence" not in signals

    def test_rsi_divergence_plus_roc_triggers(self):
        rm = _rm()
        pos = _position()
        indicators = {
            "vwap_position": "above",
            "volume_ratio": 0.9,
            "rsi_divergence": "bearish",
            "roc_deceleration": True,   # second signal active
            "roc_5": 1.0,
            "price": 105.0,
            "atr_14": 4.0,
        }
        should_exit, signals = rm.evaluate_overrides(pos, indicators, 108.0)
        assert should_exit
        assert "rsi_divergence" in signals
        assert "roc_deceleration" in signals

    def test_atr_trailing_stop_alone_triggers(self):
        rm = _rm()
        pos = _position()
        indicators = {
            "vwap_position": "above",
            "volume_ratio": 0.9,
            "rsi_divergence": False,
            "roc_deceleration": False,
            "roc_5": 1.0,
            "price": 80.0,   # drop=20 > 2*4=8
            "atr_14": 4.0,
        }
        should_exit, signals = rm.evaluate_overrides(pos, indicators, 100.0)
        assert should_exit
        assert "atr_trailing_stop" in signals

    def test_multiple_signals_all_reported(self):
        rm = _rm()
        pos = _position()
        indicators = {
            "vwap_position": "below",
            "volume_ratio": 2.0,
            "rsi_divergence": "bearish",
            "roc_deceleration": True,
            "roc_5": -2.0,
            "price": 80.0,
            "atr_14": 4.0,
        }
        should_exit, signals = rm.evaluate_overrides(pos, indicators, 100.0)
        assert should_exit
        assert "vwap_crossover" in signals
        assert "roc_deceleration" in signals
        assert "atr_trailing_stop" in signals
        assert "rsi_divergence" in signals   # has companion signals


# ---------------------------------------------------------------------------
# Settlement check
# ---------------------------------------------------------------------------

class TestSettlementCheck:
    def test_settled_position_returns_false(self):
        rm = _rm()
        portfolio = _portfolio(["AAPL"])
        # Entry was 2026-03-10, today is 2026-03-12 (T+2) — settled
        now = datetime(2026, 3, 12, 11, 0, tzinfo=ET)
        risk, msg = rm.check_settlement("AAPL", portfolio, now=now)
        assert not risk

    def test_same_day_sell_is_gfv_risk(self):
        rm = _rm()
        portfolio = _portfolio(["AAPL"])
        # Entry 2026-03-10, today is still 2026-03-10 — T+0, not settled
        now = datetime(2026, 3, 10, 14, 0, tzinfo=ET)
        risk, msg = rm.check_settlement("AAPL", portfolio, now=now)
        assert risk
        assert "GFV" in msg or "Good Faith" in msg

    def test_intraday_sell_before_settlement_is_gfv_risk(self):
        rm = _rm()
        # Entry 2026-03-11 (Tuesday). Settlement = 2026-03-12 (T+1 biz day).
        # Selling on entry day (2026-03-11) = before settlement → GFV risk.
        pos = Position(
            symbol="GOOG",
            shares=5.0,
            avg_cost=200.0,
            entry_date="2026-03-11",
            intention=TradeIntention(),
            position_id="pos_GOOG",
        )
        portfolio = PortfolioState(positions=[pos])
        now = datetime(2026, 3, 11, 14, 0, tzinfo=ET)
        risk, msg = rm.check_settlement("GOOG", portfolio, now=now)
        assert risk

    def test_no_position_returns_false(self):
        rm = _rm()
        now = datetime(2026, 3, 12, 11, 0, tzinfo=ET)
        risk, msg = rm.check_settlement("AAPL", _portfolio(), now=now)
        assert not risk

    def test_weekend_skipped_in_settlement(self):
        rm = _rm()
        # Entry Friday 2026-03-13. T+1 business day = Monday 2026-03-16
        pos = Position(
            symbol="TSLA",
            shares=5.0,
            avg_cost=100.0,
            entry_date="2026-03-13",
            intention=TradeIntention(),
            position_id="pos_TSLA",
        )
        portfolio = PortfolioState(positions=[pos])
        # Saturday 2026-03-14: NOT yet settled (settlement is Mon 2026-03-16)
        now = datetime(2026, 3, 14, 10, 0, tzinfo=ET)
        risk, _ = rm.check_settlement("TSLA", portfolio, now=now)
        assert risk
        # Monday 2026-03-16: settled
        now2 = datetime(2026, 3, 16, 11, 0, tzinfo=ET)
        risk2, _ = rm.check_settlement("TSLA", portfolio, now=now2)
        assert not risk2


# ---------------------------------------------------------------------------
# _pending_order_commitment helper
# ---------------------------------------------------------------------------

class TestPendingOrderCommitment:
    def test_sums_limit_buy_orders(self):
        orders = [
            _order(qty=10.0, limit_price=100.0, status="PENDING"),
            _order(qty=5.0, limit_price=200.0, status="PENDING", order_id="ord2"),
        ]
        assert _pending_order_commitment(orders) == 2000.0  # 10*100 + 5*200

    def test_excludes_sell_orders(self):
        orders = [_order(side="sell", qty=10.0, limit_price=100.0, status="PENDING")]
        assert _pending_order_commitment(orders) == 0.0

    def test_excludes_market_orders(self):
        orders = [_order(order_type="market", limit_price=None, status="PENDING")]
        assert _pending_order_commitment(orders) == 0.0

    def test_excludes_filled_orders(self):
        orders = [_order(qty=10.0, limit_price=100.0, status="FILLED")]
        assert _pending_order_commitment(orders) == 0.0

    def test_partially_filled_deducts_remaining(self):
        # 10 shares ordered, 4 filled, 6 remaining @ $100 → $600
        orders = [_order(qty=10.0, limit_price=100.0, status="PARTIALLY_FILLED", filled_qty=4.0)]
        assert _pending_order_commitment(orders) == 600.0


# ---------------------------------------------------------------------------
# Dead zone filtering
# ---------------------------------------------------------------------------

def _rm_with_dead_zone(start: str = "11:30", end: str = "14:30") -> RiskManager:
    """Build a RiskManager with custom dead zone times."""
    sched = SchedulerConfig(dead_zone_start_et=start, dead_zone_end_et=end)
    cfg = _cfg()
    return RiskManager(cfg, _pdt(cfg), sched)


class TestDeadZone:
    """Dead zone blocks new entries during 11:30–14:30 ET; exits unaffected."""

    def _call_hours(self, rm: RiskManager, now: datetime, blocks_eod_entries: bool = True) -> tuple[bool, str]:
        return rm._check_market_hours(blocks_eod_entries, now)

    def test_dead_zone_blocks_entry_at_noon(self):
        rm = _rm_with_dead_zone()
        # 12:00 PM ET — inside dead zone
        now = datetime(2026, 3, 11, 12, 0, tzinfo=ET)
        ok, msg = self._call_hours(rm, now)
        assert not ok
        assert "dead zone" in msg.lower()

    def test_dead_zone_allows_entry_before(self):
        rm = _rm_with_dead_zone()
        # 11:00 AM ET — before dead zone
        now = datetime(2026, 3, 11, 11, 0, tzinfo=ET)
        ok, _ = self._call_hours(rm, now)
        assert ok

    def test_dead_zone_allows_entry_after(self):
        rm = _rm_with_dead_zone()
        # 3:00 PM ET — after dead zone
        now = datetime(2026, 3, 11, 15, 0, tzinfo=ET)
        ok, _ = self._call_hours(rm, now)
        assert ok

    def test_dead_zone_boundary_start_is_blocked(self):
        rm = _rm_with_dead_zone()
        # Exactly 11:30 ET → inclusive start → blocked
        now = datetime(2026, 3, 11, 11, 30, tzinfo=ET)
        ok, msg = self._call_hours(rm, now)
        assert not ok
        assert "dead zone" in msg.lower()

    def test_dead_zone_boundary_end_is_allowed(self):
        rm = _rm_with_dead_zone()
        # Exactly 14:30 ET → exclusive end → allowed
        now = datetime(2026, 3, 11, 14, 30, tzinfo=ET)
        ok, _ = self._call_hours(rm, now)
        assert ok

    def test_dead_zone_applies_regardless_of_eod_flag(self):
        rm = _rm_with_dead_zone()
        now = datetime(2026, 3, 11, 13, 0, tzinfo=ET)
        ok, msg = self._call_hours(rm, now, blocks_eod_entries=False)
        assert not ok
        assert "dead zone" in msg.lower()

    def test_validate_entry_blocks_during_dead_zone(self):
        """Dead zone propagates through the full validate_entry path."""
        rm = _rm_with_dead_zone()
        now = datetime(2026, 3, 11, 12, 0, tzinfo=ET)
        rm._reset_daily_if_needed(_account(), now.date())
        ok, msg = rm.validate_entry(
            "AAPL", "buy", 10, 100.0, True,
            _account(), _portfolio(), [], now=now,
        )
        assert not ok
        assert "dead zone" in msg.lower()

    def test_default_rm_has_dead_zone_configured(self):
        """RiskManager without explicit scheduler uses SchedulerConfig defaults."""
        rm = _rm()  # no scheduler passed — uses defaults
        # Default dead zone is 11:30–14:30; verify noon is blocked
        now = datetime(2026, 3, 11, 12, 0, tzinfo=ET)
        ok, msg = rm._check_market_hours(True, now)
        assert not ok
        assert "dead zone" in msg.lower()


# ===========================================================================
# Short selling — validate_entry skips buying-power check
# ===========================================================================

class TestShortEntryValidation:
    """short entry (side="sell") must pass all risk checks but skip buying-power."""

    def _now_regular(self):
        """A timestamp during regular hours: Tuesday 10:00 ET."""
        return datetime(2026, 3, 10, 10, 0, tzinfo=ET)

    def test_short_entry_passes_when_buying_power_insufficient(self):
        """sell_short entries must not be blocked by the buying-power check."""
        rm = _rm()
        # Give tiny buying power — would block a long entry
        acct = _account(buying_power=1.0)
        ok, msg = rm.validate_entry(
            "TSLA", "sell", 10, 200.0, True,
            acct, _portfolio(), [], now=self._now_regular(),
        )
        assert ok, f"Short entry should pass with low buying power; got: {msg}"

    def test_long_entry_blocked_when_buying_power_insufficient(self):
        """Confirm the buying-power check still fires for long (buy) entries."""
        rm = _rm()
        acct = _account(buying_power=1.0)
        ok, msg = rm.validate_entry(
            "TSLA", "buy", 10, 200.0, True,
            acct, _portfolio(), [], now=self._now_regular(),
        )
        assert not ok
        assert "buying power" in msg.lower()

    def test_short_entry_still_blocked_by_position_limit(self):
        """Other risk checks (max concurrent positions) still apply to shorts."""
        cfg = _cfg(max_concurrent_positions=1)
        rm = _rm(cfg)
        # One position already open — at the limit
        portfolio = _portfolio(["AAPL"])
        ok, msg = rm.validate_entry(
            "TSLA", "sell", 5, 200.0, True,
            _account(), portfolio, [], now=self._now_regular(),
        )
        assert not ok
        assert "concurrent" in msg.lower()
