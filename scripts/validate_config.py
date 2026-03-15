#!/usr/bin/env python3
"""
scripts/validate_config.py
===========================
Pre-flight configuration validator for Ozymandias v3.

Checks:
  1. config.json loads and passes schema validation
  2. Credentials file exists and contains required keys
  3. Prompt template files exist for the configured version
  4. (Optional) Alpaca API connectivity
  5. (Optional) Claude API connectivity

Usage:
    python scripts/validate_config.py [--no-connectivity]
    PYTHONPATH=. python scripts/validate_config.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

# Add project root to path when run directly
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ---------------------------------------------------------------------------
# Result tracking
# ---------------------------------------------------------------------------

_results: list[tuple[str, bool, str]] = []


def _pass(label: str, detail: str = "") -> None:
    msg = f"  [PASS] {label}"
    if detail:
        msg += f"  ({detail})"
    print(msg)
    _results.append((label, True, detail))


def _fail(label: str, detail: str = "") -> None:
    msg = f"  [FAIL] {label}"
    if detail:
        msg += f"\n         {detail}"
    print(msg)
    _results.append((label, False, detail))


def _section(title: str) -> None:
    print(f"\n{title}")
    print("  " + "─" * (len(title) + 2))


# ---------------------------------------------------------------------------
# Check 1: config.json
# ---------------------------------------------------------------------------

def check_config() -> object | None:
    _section("1. Configuration file")
    try:
        from ozymandias.core.config import load_config
        cfg = load_config()
        _pass("config.json loaded and validated")
        _pass("Broker environment", cfg.broker.environment)
        _pass("Claude model", cfg.claude.model)
        _pass("Prompt version", cfg.claude.prompt_version)
        _pass(
            "Risk limits",
            f"max_pos={cfg.risk.max_position_pct:.0%}  "
            f"max_daily_loss={cfg.risk.max_daily_loss_pct:.0%}  "
            f"pdt_buffer={cfg.risk.pdt_buffer}",
        )
        ranker_sum = (
            cfg.ranker.weight_ai + cfg.ranker.weight_technical +
            cfg.ranker.weight_risk + cfg.ranker.weight_liquidity
        )
        if abs(ranker_sum - 1.0) < 0.01:
            _pass("Ranker weights sum to 1.0", f"={ranker_sum:.3f}")
        else:
            _fail("Ranker weights do not sum to 1.0", f"got {ranker_sum:.3f}")
        return cfg
    except Exception as exc:
        _fail("config.json failed to load", str(exc))
        return None


# ---------------------------------------------------------------------------
# Check 2: credentials
# ---------------------------------------------------------------------------

def check_credentials(cfg) -> dict | None:
    _section("2. Credentials file")
    try:
        creds_path = cfg.credentials_path
        if not creds_path.exists():
            _fail("Credentials file not found", str(creds_path))
            return None
        _pass("Credentials file exists", str(creds_path))

        with open(creds_path, "r", encoding="utf-8") as fh:
            creds = json.load(fh)

        api_key    = creds.get("api_key") or creds.get("APCA_API_KEY_ID")
        secret_key = creds.get("secret_key") or creds.get("APCA_API_SECRET_KEY")
        claude_key = creds.get("anthropic_api_key") or creds.get("ANTHROPIC_API_KEY")

        if api_key:
            _pass("Alpaca API key present", f"{api_key[:8]}...")
        else:
            _fail("Alpaca API key missing", "expected 'api_key' or 'APCA_API_KEY_ID'")

        if secret_key:
            _pass("Alpaca secret key present", f"{secret_key[:4]}...")
        else:
            _fail("Alpaca secret key missing", "expected 'secret_key' or 'APCA_API_SECRET_KEY'")

        if claude_key:
            _pass("Anthropic API key present", f"{claude_key[:8]}...")
        else:
            _fail("Anthropic API key missing", "expected 'anthropic_api_key'")

        return creds
    except Exception as exc:
        _fail("Credentials check failed", str(exc))
        return None


# ---------------------------------------------------------------------------
# Check 3: prompt templates
# ---------------------------------------------------------------------------

def check_prompts(cfg) -> None:
    _section("3. Prompt templates")
    try:
        prompts_dir = cfg.prompts_dir
        if not prompts_dir.exists():
            _fail(
                f"Prompts directory not found",
                str(prompts_dir),
            )
            return
        _pass("Prompts directory exists", str(prompts_dir))

        required = ["reasoning.txt", "review.txt", "watchlist.txt"]
        for name in required:
            fpath = prompts_dir / name
            if fpath.exists():
                size = fpath.stat().st_size
                _pass(f"  {name}", f"{size} bytes")
            else:
                _fail(f"  {name} missing", str(fpath))
    except Exception as exc:
        _fail("Prompt template check failed", str(exc))


# ---------------------------------------------------------------------------
# Check 4: Alpaca connectivity (optional)
# ---------------------------------------------------------------------------

async def check_alpaca_connectivity(cfg, creds: dict) -> None:
    _section("4. Alpaca API connectivity")
    try:
        from ozymandias.execution.alpaca_broker import AlpacaBroker

        api_key    = creds.get("api_key") or creds.get("APCA_API_KEY_ID", "")
        secret_key = creds.get("secret_key") or creds.get("APCA_API_SECRET_KEY", "")
        paper = cfg.broker.environment == "paper"

        broker = AlpacaBroker(api_key=api_key, secret_key=secret_key, paper=paper)
        acct = await broker.get_account()
        _pass(
            "Alpaca connected",
            f"equity=${acct.equity:,.2f}  buying_power=${acct.buying_power:,.2f}  "
            f"pdt={acct.pdt_flag}  daytrades={acct.daytrade_count}",
        )
        hours = await broker.get_market_hours()
        _pass("Market hours fetched", f"is_open={hours.is_open}  session={hours.session}")
    except Exception as exc:
        _fail("Alpaca connectivity failed", str(exc))


# ---------------------------------------------------------------------------
# Check 5: Claude connectivity (optional)
# ---------------------------------------------------------------------------

async def check_claude_connectivity(cfg, creds: dict) -> None:
    _section("5. Claude API connectivity")
    try:
        import anthropic

        claude_key = creds.get("anthropic_api_key") or creds.get("ANTHROPIC_API_KEY", "")
        if not claude_key:
            _fail("Skipping — no Anthropic API key in credentials")
            return

        client = anthropic.AsyncAnthropic(api_key=claude_key)
        resp = await client.messages.create(
            model=cfg.claude.model,
            max_tokens=32,
            messages=[{"role": "user", "content": "Reply with just the word OK."}],
        )
        reply = resp.content[0].text.strip()
        _pass("Claude API connected", f"model={cfg.claude.model}  reply={reply!r}")
    except Exception as exc:
        _fail("Claude connectivity failed", str(exc))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def _run(check_connectivity: bool) -> int:
    print("\n══ Ozymandias v3 — Configuration Validator ══\n")

    cfg = check_config()
    if cfg is None:
        print("\n[FATAL] Cannot continue without a valid config.")
        return 1

    creds = check_credentials(cfg)
    if creds is None:
        print("\n[FATAL] Cannot continue without credentials.")
        return 1

    check_prompts(cfg)

    if check_connectivity:
        await check_alpaca_connectivity(cfg, creds)
        await check_claude_connectivity(cfg, creds)
    else:
        print("\n4. Alpaca API connectivity   [skipped — use without --no-connectivity to test]")
        print("5. Claude API connectivity   [skipped]\n")

    passed = sum(1 for _, ok, _ in _results if ok)
    failed = sum(1 for _, ok, _ in _results if not ok)
    total  = len(_results)
    print(f"\n{'─' * 50}")
    if failed == 0:
        print(f"  ALL {total} CHECKS PASSED")
    else:
        print(f"  {failed}/{total} CHECKS FAILED  ({passed} passed, {failed} failed)")
    print(f"{'─' * 50}\n")
    return 1 if failed else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate Ozymandias v3 configuration")
    parser.add_argument(
        "--no-connectivity",
        action="store_true",
        help="Skip live API connectivity checks (default: checks are performed)",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(_run(not args.no_connectivity)))


if __name__ == "__main__":
    main()
