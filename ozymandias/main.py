"""
Ozymandias v3 — entry point.

Usage
-----
    python -m ozymandias.main [--config PATH] [--log-level LEVEL] [--dry-run]

Options
-------
--config PATH       Path to config.json (default: auto-discover)
--log-level LEVEL   Root log level: DEBUG | INFO | WARNING | ERROR (default: INFO)
--dry-run           Start and run loops but never place orders (logs intent only)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from ozymandias.core.config import load_config
from ozymandias.core.logger import setup_logging
from ozymandias.core.market_hours import get_current_session
from ozymandias.core.orchestrator import Orchestrator

log = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ozymandias",
        description="Automated trading bot — Ozymandias v3",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="PATH",
        help="Path to config.json (default: auto-discover in ozymandias/config/)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        metavar="LEVEL",
        help="Root log level (default: INFO)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run all loops but never place real orders",
    )
    return parser.parse_args()


def _print_banner(
    cfg,
    env: str,
    equity: float,
    watchlist_size: int,
    strategies: list[str],
) -> None:
    bar = "═" * 56
    print(f"\n{bar}")
    print(f"  Ozymandias v3")
    print(f"  Environment  : {env.upper()}")
    print(f"  Account equity: ${equity:,.2f}")
    print(f"  Watchlist    : {watchlist_size} symbol(s)")
    print(f"  Strategies   : {', '.join(strategies)}")
    print(f"  Session      : {get_current_session().value}")
    print(f"  Model        : {cfg.claude.model}")
    print(f"{bar}\n")


async def _run(args: argparse.Namespace) -> None:
    setup_logging(level=args.log_level)

    cfg = load_config(args.config)
    log.info(
        "Ozymandias v3 starting — env=%s  log_level=%s  dry_run=%s",
        cfg.broker.environment, args.log_level, args.dry_run,
    )

    orch = Orchestrator(config_path=args.config, log_level=args.log_level)

    # Register SIGTERM so systemd / container stop works identically to Ctrl-C
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(
        signal.SIGTERM,
        lambda: asyncio.ensure_future(orch._shutdown()),
    )

    # Startup banner uses state loaded before run() connects to broker
    try:
        watchlist = await orch._state_manager.load_watchlist()
        watchlist_size = len(watchlist.entries)
    except Exception:
        watchlist_size = 0

    _print_banner(
        cfg=cfg,
        env=cfg.broker.environment,
        equity=0.0,          # pre-connection placeholder; real equity logged by startup
        watchlist_size=watchlist_size,
        strategies=cfg.strategy.active_strategies,
    )

    await orch.run()


def main() -> None:
    args = _parse_args()
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        log.info("Interrupted — exiting")
        sys.exit(0)


if __name__ == "__main__":
    main()
