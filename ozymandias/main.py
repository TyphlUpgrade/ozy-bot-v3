"""
Ozymandias v3 — entry point.

Phase 01: config, state, logger, reasoning cache.
Phase 02: broker initialization and connection check.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

from ozymandias.core.config import load_config
from ozymandias.core.logger import setup_logging
from ozymandias.core.state_manager import StateManager
from ozymandias.core.reasoning_cache import ReasoningCache
from ozymandias.execution.alpaca_broker import AlpacaBroker

log = logging.getLogger(__name__)


def _load_credentials(cfg) -> tuple[str, str]:
    creds_path = cfg.credentials_path
    with open(creds_path, "r", encoding="utf-8") as fh:
        creds = json.load(fh)
    api_key = creds.get("api_key") or creds.get("APCA_API_KEY_ID")
    secret_key = creds.get("secret_key") or creds.get("APCA_API_SECRET_KEY")
    if not api_key or not secret_key:
        raise RuntimeError(
            f"credentials file at {creds_path} must contain 'api_key' and 'secret_key'"
        )
    return api_key, secret_key


async def startup() -> AlpacaBroker:
    """
    Full startup sequence.

    Phase 01: config, logging, state files, reasoning cache.
    Phase 02: broker connection verified (account info + market hours).

    Returns the ready broker instance.
    """
    # ------------------------------------------------------------------ #
    # Phase 01: core infrastructure                                        #
    # ------------------------------------------------------------------ #
    cfg = load_config()
    setup_logging()
    log.info("Ozymandias v3 starting. Model: %s  env: %s",
             cfg.claude.model, cfg.broker.environment)

    sm = StateManager()
    await sm.initialize()
    log.info("State files ready at: %s", sm._dir)

    cache = ReasoningCache()
    deleted = cache.rotate()
    log.info("Reasoning cache rotated — %d stale file(s) removed", deleted)

    fresh = cache.load_latest_if_fresh()
    if fresh:
        log.info("Fresh reasoning cache found (timestamp=%s)", fresh.get("timestamp"))
    else:
        log.info("No fresh cache — Claude will be called on first trigger")

    # ------------------------------------------------------------------ #
    # Phase 02: broker                                                     #
    # ------------------------------------------------------------------ #
    paper = cfg.broker.environment == "paper"
    api_key, secret_key = _load_credentials(cfg)

    broker = AlpacaBroker(api_key=api_key, secret_key=secret_key, paper=paper)

    acct = await broker.get_account()
    log.info(
        "Broker connected [%s] — equity=$%.2f  buying_power=$%.2f  "
        "cash=$%.2f  pdt=%s  daytrades_used=%d",
        "paper" if paper else "live",
        acct.equity, acct.buying_power, acct.cash,
        acct.pdt_flag, acct.daytrade_count,
    )

    hours = await broker.get_market_hours()
    log.info(
        "Market: is_open=%s  session=%s  next_open=%s  next_close=%s",
        hours.is_open, hours.session, hours.next_open, hours.next_close,
    )

    log.info("Startup complete.")
    return broker


def main() -> None:
    debug = "--debug" in sys.argv
    if debug:
        # Elevate stdout handler to DEBUG before setup_logging runs
        logging.basicConfig(level=logging.DEBUG)

    broker = asyncio.run(startup())

    if debug:
        # After startup, ensure the root logger's stream handler shows DEBUG
        for handler in logging.getLogger().handlers:
            if isinstance(handler, logging.StreamHandler):
                handler.setLevel(logging.DEBUG)


if __name__ == "__main__":
    main()
