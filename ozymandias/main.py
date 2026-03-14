"""
Ozymandias v3 — entry point.

Phase 01: scaffolding only. Orchestrator not yet implemented.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from ozymandias.core.config import load_config
from ozymandias.core.logger import setup_logging
from ozymandias.core.state_manager import StateManager
from ozymandias.core.reasoning_cache import ReasoningCache


async def startup_check() -> None:
    """Validate config, init state files, rotate logs and cache."""
    cfg = load_config()
    log = setup_logging()
    log.info("Ozymandias v3 starting up. Model: %s", cfg.claude.model)

    sm = StateManager()
    await sm.initialize()
    log.info("State files initialised at: %s", sm._dir)

    cache = ReasoningCache()
    deleted = cache.rotate()
    log.info("Reasoning cache rotated. Deleted %d stale files.", deleted)

    recent = cache.load_latest_if_fresh()
    if recent:
        log.info("Loaded fresh reasoning cache from %s.", recent.get("timestamp"))
    else:
        log.info("No fresh reasoning cache found. Claude will be called on first trigger.")

    log.info("Startup complete.")


def main() -> None:
    asyncio.run(startup_check())


if __name__ == "__main__":
    main()
