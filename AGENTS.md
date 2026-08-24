# AGENTS.md

Instructions for AI coding agents operating in this repository.

## Project Overview

Python CLI tool that scans for arbitrage opportunities across prediction markets. Supports one-shot scans, continuous mode with WebSocket feeds, and automated trade execution. 

**Platforms**: Polymarket, Kalshi, Betfair, Smarkets, SX Bet, Matchbook, Gemini Predictions, IBKR ForecastEx (+ Metaculus as read-only signal source)

## Commands

```bash
# Install dependencies
pip install -r requirements.txt
pip install -r requirements-dev.txt   # pytest (dev only)

# Run one-shot scan (all arb types)
python scanner.py

# Continuous mode
python scanner.py --continuous --interval 60

# Specific modes
python scanner.py --mode kalshi
python scanner.py --mode cross-all

# Execution controls
python scanner.py --dry-run                         # detect only (default)
# Live Kalshi D0: scripts/launch-kalshi-d0-live.sh
# DRY_RUN=false requires artifacts/live-envelope.json (five operator fields).
# Do not invent venue, pair, max_notional_usd, max_daily_loss_usd, or kill_switch.
```

## Testing

Tests use `pytest` with `unittest.mock`. All tests are methods inside classes (no module-level test functions). No `conftest.py` exists; shared setup uses per-file `autouse` fixtures.

```bash
# Run all tests
python -m pytest tests/ -v                      # full suite

# Run a single test file
python -m pytest tests/test_fees.py -v          # single file

# Run a single class
python -m pytest tests/test_executor.py::TestExecutor -v

# Run a single test
python -m pytest tests/test_fees.py::TestPolymarketFee::test_zero_when_sell_equals_buy -v

# Options
python -m pytest tests/ -v --tb=short           # short traceback
python -m pytest tests/ -v -x                   # stop on first failure
```

**Testing Patterns**:
- External SDKs are mocked via `sys.modules` stubs before importing the module under test.
- Tests add the project root to `sys.path`.
- `autouse` fixtures that clean `sys.modules` must only remove the specific scan module under test (e.g. `scans.gemini`), **never** `scans.helpers` or `scans.__init__` to prevent cross-test pollution.

## Code Style

No linter or formatter is configured. Style is enforced by convention only.

### Python Version & Typing
- Target **Python 3.10+**.
- Use modern union syntax: `X | None`, `list[float]`, `tuple[bool, str]`.
- **Never** use `Optional`, `List`, `Dict`, `Tuple` from `typing`.

### Naming Conventions
- Functions/methods: `snake_case` (`scan_binary_internal`, `_refine_cross_with_clob`).
- Classes: `PascalCase` (`ArbitrageExecutor`, `TradeDB`, `RiskManager`).
- Module-level constants: `UPPER_CASE` (`MIN_NET_ROI`, `MAX_TRADE_SIZE`).
- Private constants: `_UPPER_CASE` (`_CROSS_FEE_FUNCS`, `_VALID_PLATFORMS`).
- Internal dict keys: `_`-prefixed (`_token_ids`, `_clob_depth`, `_market_key`).

### Formatting & Layout
- ~120-character soft line limit; no hard enforcement.
- 4-space indentation, no tabs.
- Double quotes for strings (dominant convention).
- Section separators: Use `# ---------------------------------------------------------------------------` (75 dashes) to separate logical sections. 

### Imports
- Standard library → third-party → local, separated by blank lines.
- Relative imports within `scans/` package (`from .helpers import ...`).
- Absolute imports everywhere else (`from fees import net_profit_binary`).

### Logging & Error Handling
- Use `logging` with `%`-style formatting: `logger.info("Found %d opps in %s", count, market)`.
- `executor.py` is the sole exception (uses f-strings in log calls).
- Every module creates its own logger: `logger = logging.getLogger(__name__)`.
- **Never** add bare `except: pass`. Always log at `logger.debug()` minimum.
- Custom exceptions: `ConfigError(ValueError)` in `config.py`, `_RateLimitError(Exception)` in `kalshi_api.py` and `polymarket_api.py`.
- Guard optional dependency imports with `except ImportError`.

### Docstrings
- Google-style docstrings with `Args:` / `Returns:` sections.

### Data Structures
- Opportunities flow as **plain dicts** (not dataclass/TypedDict).
- Internal keys are `_`-prefixed.
- Public keys: `type`, `market`, `prices`, `total_cost`, `net_profit`, `net_roi`.

## Key Conventions

### scanner.py is a facade
Never add logic to `scanner.py`. It re-exports names from `scans/`, `cli.py`, `continuous.py`, and `display.py`. Tests patch `scanner.<name>` which hits these re-exports.

### Two-stage detection
All scan modules follow: mid-price scan (fast) → CLOB refinement (`_refine_*_with_clob`). The refine step drops candidates that aren't profitable at real ask prices.

### Thread safety
- `TradeDB`: threading lock + SQLite WAL mode.
- Price cache: plain dict updated from WS threads.
- Per-market locks in continuous mode prevent double execution.

### Config
`config.py` uses typed env helpers (`_env_float`, `_env_int`, `_env_bool`) that raise `ConfigError` on invalid input. Precedence: CLI args > env vars > defaults in `config.py`.

### Live Kalshi D0
The currently verified operating target is live Kalshi D0 via `scripts/launch-kalshi-d0-live.sh`. `DRY_RUN=false` fail-closes unless `artifacts/live-envelope.json` contains venue, pair, max_notional_usd, max_daily_loss_usd, and kill_switch from one operator message. Agents do not invent those fields. Other adapter-backed venues need their own validated authority, account, eligibility, limit, emergency-stop, and action-time approval gates. User-reported access is not independent eligibility verification. `kalshi_policy.live_kalshi_submit_allowed` guards every Kalshi submit path. LLM agents do not pick quotes.

## Adding a New Opportunity Type
1. Create the scan in `scans/<name>.py` following the two-stage pattern.
2. Add the fee function in `fees.py`.
3. Add a branch in `executor.py:_build_legs()` and a matching `_revalidate` case.
4. Wire into `cli.py:_run_oneshot()` and `continuous.py` if applicable.
5. Add the mode string to `cli.py` argparse choices.

## Adding a New Cross-Platform Pair
Add entries to `_CROSS_FEE_FUNCS` in `scans/cross.py` using `functools.partial(net_profit_cross_generic, buy_fee, sell_fee)`.

## Files to Never Read or Commit
`.env`, `.env.*`, `*.pem`, `*.key`, `*credential*`, `secrets/*`

## CI / CD & Deployment
- `.github/workflows/test.yml` — correctness lint plus pytest on PRs to `master` (Python 3.12); any failure or collection crash fails CI.
- Railway auto-deploys on push to `master` via GitHub integration. Dockerfile-based build (`python:3.12-slim`).
