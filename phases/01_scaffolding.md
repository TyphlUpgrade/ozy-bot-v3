# Phase 01: Project Scaffolding + State Management + Logging + Market Hours

Read the full spec at `ozymandias_v3_spec_revised.md` — specifically sections 5 (Persistent State), 6 (Logging), 4.9 (Market Hours), and 10 (Directory Structure).

## What to Build

### 1. Directory structure
Create the full directory structure from section 10 of the spec. Create empty `__init__.py` files where needed. Create a `requirements.txt` per section 11.

### 2. Configuration (`core/config.py` + `config/config.json`)
- Create the `config/config.json` file with the full default configuration from section 5.3 of the spec.
- Create a `core/config.py` module that loads, validates, and provides typed access to config values. Use dataclasses or Pydantic for type safety. It should load from `config/config.json` and provide sensible defaults for any missing keys.
- Create a stub `config/credentials.enc` (for now, just a JSON file with placeholder keys — encryption is not needed for paper trading).

### 3. State manager (`core/state_manager.py`)
This is the foundation everything else depends on. Implement per section 5 of the spec:
- JSON read/write with **atomic writes**: write to a temp file in the same directory, then `os.replace()` to the target path. This prevents corruption if the process crashes mid-write.
- Schema validation on load. Define expected schemas for `portfolio.json`, `watchlist.json`, and `orders.json`. If a file fails validation, raise a clear error and refuse to start.
- Initialize empty state files on first run if they don't exist.
- Provide typed read/write methods: `load_portfolio()`, `save_portfolio()`, `load_watchlist()`, `save_watchlist()`, `load_orders()`, `save_orders()`.
- Use `asyncio.Lock` per state file to prevent concurrent writes.

Define the data models (dataclasses) for:
- `Position` with full trade intention fields (section 5.1)
- `WatchlistEntry` with all fields (section 5.2)
- `OrderRecord` with all fields including partial fill tracking (section 5.4)
- `PortfolioState`, `WatchlistState`, `OrdersState` as top-level containers

### 4. Logger (`core/logger.py`)
Implement per section 6:
- Two-file rotation: `logs/current.log` and `logs/previous.log`.
- On startup: rename `current.log` → `previous.log` (overwrite old previous), create fresh `current.log`.
- Use Python's `logging` module with a custom formatter.
- Format: ISO 8601 UTC timestamp, module name, log level, structured message.
- Provide a `setup_logging()` function that configures the root logger and returns it.

### 5. Reasoning cache manager
Implement per section 5.5:
- Save Claude responses as `reasoning_cache/reasoning_{timestamp_utc}.json`.
- On startup: delete cache files older than the previous session.
- Startup reuse: if a cached response from today exists and is <60 minutes old, load it.
- Max 30 files per session; delete oldest if exceeded.

### 6. Market hours (`core/market_hours.py`)
Implement per section 4.9:
- `get_current_session()` returning one of: `pre_market`, `regular_hours`, `post_market`, `closed`.
- All comparisons use US/Eastern time via `zoneinfo.ZoneInfo("America/New_York")`.
- Handle the "last 5 minutes" special case (3:55-4:00 PM ET).
- Weekend detection.
- **Do not** implement holiday detection yet — just add a `# TODO: NYSE holiday calendar` comment. We'll handle that later.
- Every function should accept an optional `now: datetime` parameter for testability, defaulting to `datetime.now(ZoneInfo("America/New_York"))`.

## Tests to Write

Create `tests/test_state_manager.py`:
- Test atomic write (simulate crash by checking temp file behavior)
- Test schema validation catches malformed data
- Test load/save roundtrip for each state type
- Test concurrent write safety (two async tasks writing simultaneously)
- Test first-run initialization creates empty valid state files

Create `tests/test_market_hours.py`:
- Test each session detection with hardcoded timestamps
- Test the 3:55 PM boundary
- Test weekend detection
- Test that timezone handling is correct (pass UTC times, verify ET conversion)

Create `tests/test_logger.py`:
- Test log rotation on startup
- Test log format output

## Done When
- `pytest tests/test_state_manager.py tests/test_market_hours.py tests/test_logger.py` all pass
- You can run a quick script that creates state files, writes to them, reads them back, and verifies integrity
- The config loads and provides typed access to all fields
