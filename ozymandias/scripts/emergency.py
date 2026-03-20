"""
Emergency operator commands.

Usage
-----
    python -m ozymandias.scripts.emergency exit      # liquidate all positions
    python -m ozymandias.scripts.emergency shutdown  # stop the bot

Both commands work by writing a signal file that the running bot detects on
its next fast-loop tick (~5-15 seconds). The file is deleted automatically
after the bot processes it.

For Discord integration: write the appropriate signal file from your Discord
bot handler instead of running this script.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ozymandias.core.orchestrator import EMERGENCY_EXIT_SIGNAL, EMERGENCY_SHUTDOWN_SIGNAL

_SIGNALS = {
    "exit":     (EMERGENCY_EXIT_SIGNAL,     "Liquidate all positions"),
    "shutdown": (EMERGENCY_SHUTDOWN_SIGNAL, "Stop the bot"),
}


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="ozymandias.scripts.emergency",
        description="Send an emergency command to the running bot",
    )
    parser.add_argument(
        "command",
        choices=list(_SIGNALS),
        help="exit: sell all positions immediately | shutdown: stop the bot",
    )
    parser.add_argument(
        "--yes", "-y",
        action="store_true",
        help="Skip confirmation prompt",
    )
    args = parser.parse_args()

    signal_path, description = _SIGNALS[args.command]

    if not args.yes:
        confirm = input(f"Confirm: {description}? [y/N] ").strip().lower()
        if confirm != "y":
            print("Aborted.")
            sys.exit(0)

    try:
        signal_path.parent.mkdir(parents=True, exist_ok=True)
        signal_path.touch()
    except OSError as exc:
        print(f"ERROR: could not write signal file {signal_path}: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Signal written: {signal_path}")
    print(f"The bot will {description.lower()} within ~15 seconds.")


if __name__ == "__main__":
    main()
