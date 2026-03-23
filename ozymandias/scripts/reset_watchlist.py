"""
reset_watchlist.py — replace or clear the watchlist without running the bot.

Usage:
  # Replace watchlist with specific symbols (tier 1 by default):
  python -m ozymandias.scripts.reset_watchlist AAPL MSFT NVDA

  # Clear watchlist entirely (triggers immediate watchlist_small on next startup):
  python -m ozymandias.scripts.reset_watchlist --empty

  # Preview changes without writing:
  python -m ozymandias.scripts.reset_watchlist AAPL MSFT --dry-run
  python -m ozymandias.scripts.reset_watchlist --empty --dry-run

All writes go through StateManager (atomic: write temp → rename) and validate
the schema before committing. The existing watchlist is printed before and after.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure project root is on the path when run as a script
_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT))

from ozymandias.core.config import load_config
from ozymandias.core.state_manager import StateManager, WatchlistEntry, WatchlistState


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replace or clear the Ozymandias watchlist.",
        epilog=(
            "Examples:\n"
            "  python -m ozymandias.scripts.reset_watchlist AAPL MSFT NVDA\n"
            "  python -m ozymandias.scripts.reset_watchlist --empty\n"
            "  python -m ozymandias.scripts.reset_watchlist AAPL MSFT --tier 2 --dry-run"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "symbols",
        nargs="*",
        metavar="SYMBOL",
        help="Symbols to add to the watchlist. Mutually exclusive with --empty.",
    )
    parser.add_argument(
        "--empty",
        action="store_true",
        help="Clear the watchlist entirely (triggers watchlist_small on next startup).",
    )
    parser.add_argument(
        "--tier",
        type=int,
        choices=[1, 2],
        default=1,
        help="priority_tier for positional SYMBOL args (default: 1).",
    )
    parser.add_argument(
        "--strategy",
        choices=["momentum", "swing", "both"],
        default="both",
        help="Strategy hint for positional SYMBOL args (default: both).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would change without writing.",
    )
    return parser.parse_args()


async def main() -> None:
    args = _parse_args()

    if args.empty and args.symbols:
        print("ERROR: --empty and symbol list are mutually exclusive.", file=sys.stderr)
        sys.exit(1)
    if not args.empty and not args.symbols:
        print("ERROR: provide at least one SYMBOL or use --empty.", file=sys.stderr)
        sys.exit(1)

    cfg = load_config()
    sm = StateManager(cfg._config_dir / ".." / "state")
    current = await sm.load_watchlist()

    print(f"Current watchlist ({len(current.entries)} entries):")
    for e in current.entries:
        print(f"  [{e.priority_tier}] {e.symbol}")

    if args.empty:
        new_entries: list[WatchlistEntry] = []
        print("\nAction: CLEAR watchlist entirely.")
    else:
        now_iso = datetime.now(timezone.utc).isoformat()
        new_entries = [
            WatchlistEntry(
                symbol=sym.strip().upper(),
                priority_tier=args.tier,
                strategy=args.strategy,
                date_added=now_iso,
                reason="Manual reset via reset_watchlist.py",
            )
            for sym in args.symbols
            if sym.strip()
        ]
        print(f"\nAction: REPLACE watchlist with {len(new_entries)} symbol(s):")
        for e in new_entries:
            print(f"  [{e.priority_tier}] {e.symbol}  strategy={e.strategy}")

    if args.dry_run:
        print("\n[dry-run] No changes written.")
        return

    new_watchlist = WatchlistState(entries=new_entries)
    await sm.save_watchlist(new_watchlist)

    print(f"\nWatchlist reset. {len(new_entries)} entries written.")
    if not new_entries:
        print("The watchlist_small trigger will fire on next startup and rebuild the list.")


if __name__ == "__main__":
    asyncio.run(main())
