# Phase 9: Structural Alpha Strategies - Research

**Researched:** 2026-04-05
**Domain:** High-complexity structural alpha strategies (semantic logical arbitrage, whale copy trading)
**Confidence:** HIGH for architecture patterns; MEDIUM for Polygonscan API integration details

## Summary

Phase 9 implements two advanced structural alpha strategies with significant detection and execution complexity: combinatorial logical arbitrage (detects semantic inconsistencies across related markets) and whale copy trading (mirrors profitable Polymarket wallets via on-chain monitoring). Both extend the existing Phase 8 scan architecture with new modules, fee calculators, execution legs, and dashboard integration.

**Primary recommendation:** Follow proven Phase 8 patterns (two-stage scan with mid-price candidate identification and CLOB refinement) for both strategies. Logical arb uses config-driven semantic rules (JSON), whale copy uses Polygonscan API to monitor contract events. Both are Layer 4 informed trading with 10% revalidation floor and strict position sizing ($15-20 per trade).

## User Constraints (from CONTEXT.md)

### Locked Decisions

**Combinatorial Logical Arb (STRAT-04)**
- NLP-free approach: manual semantic rules mapping related markets (e.g., "Bitcoin >$100k" implies "Bitcoin >$90k")
- Config-driven rule sets in JSON format: `{"if_yes": "market_A_id", "then_yes": "market_B_id", "relationship": "implies"}`
- Loaded from LOGICAL_ARB_RULES env var (JSON string) or `logical_arb_rules.json` file
- Signal threshold: price of implied market < price of implying market by >5% = opportunity
- Execution: buy the underpriced implied outcome, Layer 4 revalidation (10% floor), max $20 per position
- New scan module: `scans/logical_arb.py`

**Whale Copy Trading (STRAT-05)**
- Polygonscan API for Polymarket CLOB contract events — monitor specific wallet addresses for large trades
- Manual list of profitable wallets in config (WHALE_WALLETS env var, comma-separated addresses)
- Latency budget: <30s from on-chain trade detection to mirror order placement
- Max $15 per mirror trade, Layer 4 revalidation (10% floor), 5 concurrent whale-copy positions max
- New scan module: `scans/whale_copy.py`, new API client: `polygonscan_api.py`

**Dashboard Integration**
- Both strategies appear in Phase 6 monitoring dashboard with their own P&L attribution rows
- Extend dashboard_ui.py with 2 new leaderboard rows
- Extend dashboard.py /status endpoint with strategy metrics

**Shared Infrastructure**
- Each strategy gets: scan module, fee function, _build_legs branch, _revalidate case, CLI mode, continuous mode entry
- Feature flags: LOGICAL_ARB_ENABLED, WHALE_COPY_ENABLED (default false)
- All config vars use existing _env_* helpers with sensible defaults

### Claude's Discretion

- Polygonscan API integration details (authentication, rate limits, event parsing)
- Semantic rule parsing and validation logic
- Wallet trade event format and filtering
- Dashboard layout for 2 new strategy rows
- Test structure and fixtures

### Deferred Ideas (OUT OF SCOPE)

- ML-based semantic relationship detection (use manual rules for now)
- On-chain MEV-style frontrunning (not appropriate for prediction markets)
- Cross-chain wallet tracking (Polymarket on Polygon only for now)
- Social graph analysis of whale traders (use static wallet list)

## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| STRAT-04 | Combinatorial/logical arb scan detects semantic inconsistencies across related markets and logs opportunity events | Semantic rules in config, two-stage scan with Polymarket CLOB refinement, Layer 4 fee structure |
| STRAT-05 | Whale copy trading monitors profitable Polymarket wallets on-chain and triggers mirror positions within <30s latency budget | Polygonscan API event monitoring, wallet address tracking, CLOB order placement within revalidation budget |

## Standard Stack

### Core Libraries (Existing)

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| requests | 2.31.0 | HTTP requests for Polygonscan API | Proven in 10+ API clients (Finnhub, Kalshi, Betfair, etc.) |
| py-clob-client | 0.34.5 | Polymarket CLOB trading | Phase 1 baseline, already battle-tested |
| tenacity | 9.1.4 | Exponential backoff retries | Phase 1 baseline, handles 429 rate limits |
| thefuzz | 0.22.1 | Fuzzy string matching for semantic rule validation | Phase 2 baseline, existing in codebase |

### New Dependencies (Phase 9 Additions)

| Library | Version | Purpose | Why Needed |
|---------|---------|---------|-----------|
| None required | — | All Phase 9 APIs use existing requests + JSON parsing | Follows DRY principle; no external blockchain library needed |

**Note:** Phase 9 does NOT require `web3.py` or other blockchain SDKs. Polygonscan API is a REST interface queried via `requests`, same pattern as 10 existing API clients. [VERIFIED: existing polymarket_api.py, kalshi_api.py, finnhub_api.py use requests only]

**Installation:**
```bash
# No new packages required; existing requirements.txt covers all dependencies
# Verify requests and tenacity are in requirements.txt (they are)
pip install -r requirements.txt
```

### Configuration Pattern (Existing)

Both strategies follow config.py patterns from Phase 8:

```python
# Feature flags
LOGICAL_ARB_ENABLED = _env_bool("LOGICAL_ARB_ENABLED", "false")
WHALE_COPY_ENABLED = _env_bool("WHALE_COPY_ENABLED", "false")

# Strategy parameters
LOGICAL_ARB_RULES = _env_json("LOGICAL_ARB_RULES", default="[]")  # or load from file
LOGICAL_ARB_PRICE_THRESHOLD = _env_float("LOGICAL_ARB_PRICE_THRESHOLD", "0.05")  # 5% discount
LOGICAL_ARB_MAX_TRADE_SIZE = _env_float("LOGICAL_ARB_MAX_TRADE_SIZE", "20.0")

WHALE_WALLETS = _env_str("WHALE_WALLETS", "").split(",")  # Comma-separated addresses
WHALE_COPY_MAX_TRADE_SIZE = _env_float("WHALE_COPY_MAX_TRADE_SIZE", "15.0")
WHALE_COPY_MAX_POSITIONS = _env_int("WHALE_COPY_MAX_POSITIONS", "5")
POLYGONSCAN_API_KEY = _env_str("POLYGONSCAN_API_KEY", "")  # Free or Pro tier
WHALE_COPY_POLL_INTERVAL = _env_int("WHALE_COPY_POLL_INTERVAL", "10")  # seconds
```

[VERIFIED: Phase 8 uses identical pattern — see config.py IMBALANCE_ENABLED, NEWS_SNIPE_ENABLED, CORRELATED_ENABLED, TIME_DECAY_ENABLED]

## Architecture Patterns

### Recommended Project Structure

```
scans/
├── logical_arb.py          # NEW: Combinatorial logical arbitrage scan
├── whale_copy.py           # NEW: Whale copy trading scan
├── imbalance.py            # Phase 8 reference pattern
├── news_snipe.py           # Phase 8 reference pattern
├── correlated.py           # Phase 8 reference pattern
├── time_decay.py           # Phase 8 reference pattern

polygonscan_api.py          # NEW: Polygonscan REST API client

executor.py
├── _build_legs()          # Add "LogicalArb" and "WhaleCopy" branches
├── _revalidate_*()        # Add corresponding revalidation cases
└── [existing 20+ types]   # Binary, Cross, KalshiBinary, etc.

fees.py
├── net_profit_logical_arb() # NEW: Fee function for logical arb
├── net_profit_whale_copy()  # NEW: Fee function for whale copy
└── [existing 18+ functions] # Binary, Kalshi, Cross, etc.

config.py
├── LOGICAL_ARB_ENABLED
├── WHALE_COPY_ENABLED
├── LOGICAL_ARB_RULES
├── WHALE_WALLETS
├── POLYGONSCAN_API_KEY
└── [36 existing flags]

cli.py
├── choices=["all", ..., "logical-arb", "whale-copy"] # NEW modes
└── [existing modes]

continuous.py
├── if args.mode in ("all", "logical-arb") and CONFIG_LOGICAL_ARB_ENABLED: # NEW
├── if args.mode in ("all", "whale-copy") and CONFIG_WHALE_COPY_ENABLED:   # NEW
└── [existing 20+ mode handlers]

dashboard.py / dashboard_ui.py
├── LeaderboardRow "LogicalArb"  # NEW
├── LeaderboardRow "WhaleCopy"   # NEW
└── [existing 20+ rows]
```

### Pattern 1: Two-Stage Scan Architecture (Logical Arb)

**What:** Logical arb follows Phase 8 pattern: Stage 1 finds candidate markets via semantic rule matching at mid prices; Stage 2 refines against live CLOB order book, discarding candidates where spread widened >30%.

**When to use:** Any strategy that needs fast candidate identification followed by precise cost validation. Proven in Phase 8 (imbalance, news snipe, correlated, time decay).

**Example:**

```python
# Source: scans/imbalance.py (Phase 8 reference), adapted for logical_arb.py

def scan_logical_arb(
    markets_by_key: dict,
    logical_arb_rules: list[dict],
    price_threshold: float = 0.05,
) -> list[dict]:
    """Stage 1: Scan for semantic rule violations at mid prices."""
    opportunities = []
    
    from scans.helpers import _extract_token_ids
    
    # Build rule index for fast lookups
    rule_index = {}
    for rule in logical_arb_rules:
        if_market = rule.get("if_yes")
        then_market = rule.get("then_yes")
        rule_index[if_market] = (then_market, rule.get("relationship"))
    
    # Scan each market pair in the rule set
    for if_market_id, (then_market_id, relationship) in rule_index.items():
        if_market = markets_by_key.get(f"polymarket-{if_market_id}")
        then_market = markets_by_key.get(f"polymarket-{then_market_id}")
        
        if not if_market or not then_market:
            continue
        
        # Get mid prices (Stage 1: fast, approximate)
        if_price = if_market.get("price", 0.5)
        then_price = then_market.get("price", 0.5)
        
        # Detect opportunity: if relationship is "implies", then_price should be >= if_price
        # e.g., "Bitcoin >$100k" implies "Bitcoin >$90k", so P(>$100k) ≤ P(>$90k)
        if relationship == "implies" and then_price < if_price * (1 - price_threshold):
            # then_price is significantly lower → buy "then" outcome, sell "if" outcome
            opportunities.append({
                "type": "LogicalArb",
                "market": f"{if_market['question']} → {then_market['question']}",
                "if_market_id": if_market_id,
                "then_market_id": then_market_id,
                "_if_price": if_price,
                "_then_price": then_price,
                "_token_ids": _extract_token_ids(then_market),
                "_market_key": then_market_id,
                "_layer": 4,
            })
    
    # Stage 2: Refine with CLOB depth check
    opportunities = _refine_logical_arb_with_clob(opportunities)
    return opportunities


def _refine_logical_arb_with_clob(opportunities: list[dict]) -> list[dict]:
    """Stage 2: Validate prices against live CLOB order book."""
    if not opportunities:
        return opportunities
    
    from polymarket_api import fetch_order_book
    
    refined = []
    for opp in opportunities:
        token_ids = opp.get("_token_ids", [])
        if not token_ids:
            continue
        
        # Re-fetch live prices from CLOB
        then_yes_book = fetch_order_book(token_ids[0])  # YES token for "then" market
        if not then_yes_book:
            refined.append(opp)  # Graceful degradation
            continue
        
        # Check that spread hasn't blown out (>30% price increase)
        ask_price = then_yes_book.get("asks", [{}])[0].get("price", opp["_then_price"])
        if ask_price > opp["_then_price"] * 1.3:  # Spread widened >30%
            logger.debug("Logical arb spread widened, dropping")
            continue
        
        opp["_clob_ask_price"] = ask_price
        refined.append(opp)
    
    return refined
```

[CITED: Phase 8 patterns from scans/imbalance.py, scans/news_snipe.py, scans/correlated.py]

### Pattern 2: On-Chain Wallet Monitoring (Whale Copy)

**What:** Whale copy uses Polygonscan API to poll for OrderFilled events from specific wallet addresses on the Polymarket CLOB contract. When a whale trades >threshold size, the scanner immediately creates a mirror opportunity.

**When to use:** Any strategy that requires on-chain event monitoring or wallet activity tracking. Latency is critical (<30s detection-to-execution).

**Example:**

```python
# Source: polygonscan_api.py (NEW)

from tenacity import retry, stop_after_attempt, wait_exponential
import requests
import logging
import time

logger = logging.getLogger(__name__)

class PolygonscanClient:
    """Polygonscan REST API client for wallet event monitoring."""
    
    def __init__(self, api_key: str = ""):
        """Initialize Polygonscan client.
        
        Args:
            api_key: Polygonscan API key (free or Pro tier).
                    If empty, falls back to public endpoint (5 req/sec limit).
        """
        self.api_key = api_key
        self.base_url = "https://api.polygonscan.com/api"
        self._session = requests.Session()
        self._request_timeout = 10.0
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout)),
        reraise=True,
    )
    def get_latest_transactions(
        self,
        address: str,
        start_block: int = 0,
        sort: str = "desc",  # Newest first
    ) -> list[dict]:
        """Fetch latest transactions for a wallet address.
        
        Args:
            address: Polygon wallet address (0x...).
            start_block: Start block number (0 = from genesis).
            sort: "asc" or "desc" (newest first for real-time detection).
        
        Returns:
            List of transaction dicts with keys: hash, from, to, value, gas, isError, etc.
        """
        params = {
            "module": "account",
            "action": "txlist",
            "address": address,
            "startblock": start_block,
            "endblock": 99999999,
            "sort": sort,
            "apikey": self.api_key or "YourApiKeyToken",  # Free tier uses this
        }
        
        try:
            resp = self._session.get(
                self.base_url,
                params=params,
                timeout=self._request_timeout,
            )
            
            if resp.status_code == 429:
                logger.warning("Polygonscan rate limited (429), will retry")
                raise requests.Timeout("Rate limit exceeded")
            
            resp.raise_for_status()
            data = resp.json()
            
            if data.get("status") == "0":
                # No transactions or error
                logger.debug("No transactions for %s: %s", address, data.get("message"))
                return []
            
            transactions = data.get("result", [])
            logger.info("Fetched %d transactions for %s", len(transactions), address)
            return transactions
        
        except Exception as e:
            logger.error("Polygonscan fetch failed: %s", e)
            raise


# Source: scans/whale_copy.py (NEW)

def scan_whale_copy(
    whale_wallets: list[str],
    polygonscan_client,
    last_block_cache: dict | None = None,
) -> list[dict]:
    """Stage 1: Poll Polygonscan for whale wallet trades on CLOB contract.
    
    Detects recent OrderFilled events from tracked wallets, then mirrors
    the trade on our account if profitable.
    """
    if not whale_wallets or not polygonscan_client:
        return []
    
    opportunities = []
    
    for wallet in whale_wallets:
        try:
            # Fetch latest transactions for this wallet
            txs = polygonscan_client.get_latest_transactions(
                address=wallet,
                start_block=last_block_cache.get(wallet, 0) if last_block_cache else 0,
                sort="desc",
            )
            
            # Filter for CLOB contract interactions (to=0x4bfb41d5...)
            clob_txs = [
                tx for tx in txs
                if tx.get("to", "").lower() == POLYMARKET_CLOB_ADDRESS.lower()
            ]
            
            # Decode transaction input to extract trade details
            for tx in clob_txs:
                opp = _parse_clob_transaction(tx, wallet)
                if opp:
                    opportunities.append(opp)
            
            # Update last seen block for this wallet
            if txs and last_block_cache is not None:
                last_block_cache[wallet] = int(txs[0].get("blockNumber", 0))
        
        except Exception as e:
            logger.warning("Failed to fetch wallet %s: %s", wallet, e)
            continue
    
    logger.info("Whale copy: found %d opportunities from %d wallets", len(opportunities), len(whale_wallets))
    
    # Stage 2: Refine with current market prices
    opportunities = _refine_whale_copy_with_prices(opportunities)
    return opportunities


def _parse_clob_transaction(tx: dict, wallet: str) -> dict | None:
    """Extract trade details from CLOB transaction.
    
    Returns opportunity dict with type="WhaleCopy", or None if not a trade.
    """
    # This requires decoding the transaction input (calldata)
    # For MVP: simplified — just parse the value and timestamp
    # Real implementation would decode the function signature and args
    
    return {
        "type": "WhaleCopy",
        "market": f"Whale trade from {wallet[:8]}... at block {tx.get('blockNumber')}",
        "_whale_address": wallet,
        "_whale_tx_hash": tx.get("hash"),
        "_whale_timestamp": int(tx.get("timeStamp", 0)),
        "_whale_block": int(tx.get("blockNumber", 0)),
        "_market_key": tx.get("hash"),  # Use tx hash as unique key
        "_layer": 4,
    }
```

[VERIFIED: Polygonscan API reference — https://polygonscan.com/apis for free tier, https://docs.polygonscan.com for detailed endpoints]
[CITED: Polymarket CLOB contract address 0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e on Polygon — https://polygonscan.com/address/0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e]

### Integration with Executor

Both strategies require new branches in `executor.py:_build_legs()` and matching `_revalidate_*()` cases:

```python
# Source: executor.py (add these branches)

elif opp_type == "LogicalArb":
    # Buy underpriced "then" outcome, sell "if" outcome (risk hedge)
    legs = [
        {"platform": "polymarket", "side": "yes", "action": "buy",
         "price": opp["_then_price"], "_token_id": opp["_token_ids"][0]},
        {"platform": "polymarket", "side": "no", "action": "sell",
         "price": opp["_if_price"], "_token_id": opp["_if_token_ids"][0]},
    ]

elif opp_type == "WhaleCopy":
    # Mirror the whale's trade direction (buy or sell YES)
    direction = opp.get("_whale_direction", "yes")  # Parsed from tx
    legs = [
        {"platform": "polymarket", "side": direction, "action": "buy",
         "price": opp["_market_price"], "_token_id": opp["_token_ids"][0]},
    ]

# Corresponding revalidation cases:
def _revalidate_logical_arb(self, opp: dict) -> bool:
    """Revalidate logical arb: check both markets' prices haven't moved >10%."""
    # [revalidation logic with Layer 4 10% floor]

def _revalidate_whale_copy(self, opp: dict) -> bool:
    """Revalidate whale copy: check market price still within latency budget."""
    # [revalidation logic with Layer 4 10% floor, <30s detection-to-execution]
```

[CITED: executor.py lines 1135-1550 show existing _build_legs pattern for 20+ opportunity types]

### Anti-Patterns to Avoid

- **Hard-coding semantic rules in code:** Rules are config-driven JSON, loaded from env var. Future rule updates don't require code change.
- **Polling Polygonscan faster than 5 req/sec:** The free tier rate limit is strict. Use exponential backoff with tenacity (already imported).
- **Missing CLOB refinement for logical arb:** Stage 1 mid-price detection is fast but inaccurate; Stage 2 CLOB validation is mandatory.
- **Whale copy without revalidation:** 30s latency budget is tight. Must revalidate market prices within 10% floor before execution.
- **Assuming Polygonscan API key is set:** Fall back to free tier (slower, 5 req/sec limit) or graceful degradation (log error, skip scan).
- **Mixing on-chain event parsing with order matching:** Keep wallet monitoring (Polygonscan) separate from market matching (CLOB refinement).

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| REST API client for blockchain explorer | Custom HTTP wrapper | Use tenacity + requests pattern (proven in 10 existing clients) | Rate limit handling, retries, session management are non-trivial |
| Semantic rule matching | Custom NLP or regex parser | Config-driven JSON rules with fuzzy title matching | NLP overfits; manual rules are maintainable and auditable |
| Wallet event detection | WebSocket subscription to Polygonscan | Polygonscan REST API polling (free tier available) | Polygonscan doesn't offer WebSocket for free tier; REST polling is sufficient for <30s latency |
| Transaction calldata decoding | Manual ABI parsing | py-clob-client's existing order parsing utilities | CLOB function signatures are complex; library handles versioning |
| Rate limit backoff | Naive sleep loop | tenacity library (already imported, Phase 1 baseline) | Exponential backoff is stateful; tenacity handles all edge cases |
| Whale address tracking | Hardcoded list in code | WHALE_WALLETS env var + comma-separated config | Future additions require no code change; follows DRY principle |

**Key insight:** Phase 9 reuses infrastructure (requests, tenacity, py-clob-client, fuzzy matching, CLOB refinement pattern) rather than building new tools. The complexity is in orchestration and business logic, not infrastructure.

## Common Pitfalls

### Pitfall 1: Latency Explosion in 30s Whale Copy Budget

**What goes wrong:** Polygonscan API returns transaction after 5s, then CLOB revalidation takes 3s, then order matching takes 2s, then execution takes 5s — total 15s is fine. But with retry loops and queue delays, hitting 30s+ timeout is common.

**Why it happens:** No time tracking throughout the scan-revalidate-execute pipeline. Slow CLOB fetches pile up without visibility.

**How to avoid:** 
1. Log timestamps at each stage (Polygonscan fetch, CLOB fetch, order placement, fill confirmation).
2. Implement strict timeout on CLOB refinement step (5s max).
3. Skip opportunity if detection-to-execution time exceeds 20s (leaving 10s buffer).
4. Monitor `time_to_fill` metric in dashboard to detect creeping latency.

**Warning signs:** Dashboard shows whale copy opportunities but zero executions; logs show "timed out waiting for revalidation".

### Pitfall 2: Polygonscan Rate Limit Causes Silent Failures

**What goes wrong:** Free tier is 5 req/sec. Polling 10 wallets every 10 seconds = 1 req/sec per wallet, fine. But with retries after transient failures, you hit 429 (too many requests) and lose that scan cycle. No error logged to user-facing dashboard, so they don't know data is stale.

**Why it happens:** Exponential backoff is implemented but the skipped scan cycle isn't visible. Next scan happens on schedule, looks successful, but whale events during the gap are missed.

**How to avoid:**
1. Implement hard limit on requests per second at client level (throttle before sending request).
2. Log rate limit hits with wallet address (helps identify problematic wallets).
3. If rate limited, mark wallet as temporarily unavailable (don't retry immediately).
4. Dashboard /status should expose "whale_copy_rate_limit_events_24h" metric.

**Warning signs:** Logs show "429 rate limited" but then immediately succeed; whale copy P&L is inconsistent.

### Pitfall 3: Semantic Rule Conflicts Cause Negative Trades

**What goes wrong:** Rules are user-supplied JSON. If two rules conflict (e.g., rule A says "Bitcoin >$100k" implies "Bitcoin >$90k", rule B says ">$90k" implies ">$100k"), the scanner creates trades that offset each other and lose to fees.

**Why it happens:** No validation of rule consistency at config load time. Rules are assumed correct.

**How to avoid:**
1. Validate rule graph for cycles and contradictions at startup (config.validate_config).
2. Log warning if two rules reference the same market pair in opposite directions.
3. Provide a test mode (--test-logical-arb-rules) that loads rules and reports conflicts.
4. Dashboard should show rule source and validation status.

**Warning signs:** Logical arb creates tiny opposite trades on same market pair; net P&L is negative.

### Pitfall 4: CLOB Refinement Timeout Wastes Opportunities

**What goes wrong:** Logical arb Stage 2 calls fetch_order_book() for each candidate. If the API is slow (3s+ per call), and you have 20 candidates, refinement takes 60s. By then, prices have moved and the opportunity is stale.

**Why it happens:** CLOB refinement is sequential, not parallelized. No timeout on individual book fetches.

**How to avoid:**
1. Parallelize CLOB refinement using ThreadPoolExecutor (Phase 5 baseline).
2. Set hard timeout (2s) on individual fetch_order_book calls.
3. If timeout, keep opportunity but flag as "unrefined" and apply wider safety margin.
4. Log slow fetches to identify problematic markets.

**Warning signs:** Logical arb finds candidates but refinement phase is slow; revalidation often rejects due to stale prices.

## Code Examples

Verified patterns from existing codebase:

### Feature Flag Pattern (Logical Arb)

```python
# Source: config.py (Phase 8 reference)

LOGICAL_ARB_ENABLED = _env_bool("LOGICAL_ARB_ENABLED", "false")

if LOGICAL_ARB_ENABLED:
    # Load rules from env var or file
    try:
        logical_arb_rules_str = os.getenv("LOGICAL_ARB_RULES", "")
        if logical_arb_rules_str:
            LOGICAL_ARB_RULES = json.loads(logical_arb_rules_str)
        else:
            # Fallback to file
            rule_file = "logical_arb_rules.json"
            if os.path.exists(rule_file):
                with open(rule_file) as f:
                    LOGICAL_ARB_RULES = json.load(f)
            else:
                LOGICAL_ARB_RULES = []
                ConfigError("LOGICAL_ARB_ENABLED but no rules provided")
    except json.JSONDecodeError as e:
        raise ConfigError(f"Invalid LOGICAL_ARB_RULES JSON: {e}")

LOGICAL_ARB_PRICE_THRESHOLD = _env_float("LOGICAL_ARB_PRICE_THRESHOLD", "0.05")
LOGICAL_ARB_MAX_TRADE_SIZE = _env_float("LOGICAL_ARB_MAX_TRADE_SIZE", "20.0")
```

[CITED: config.py Phase 8 pattern — IMBALANCE_ENABLED, NEWS_SNIPE_ENABLED, CORRELATED_ENABLED]

### API Client Pattern (Whale Copy)

```python
# Source: finnhub_api.py (Phase 8 reference), adapted for Polygonscan

from tenacity import retry, stop_after_attempt, wait_exponential
import requests

class PolygonscanClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self._session = requests.Session()
        self._session.mount("https://", HTTPAdapter(pool_connections=2, pool_maxsize=10))
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout)),
    )
    def get_latest_transactions(self, address: str) -> list[dict]:
        resp = self._session.get(
            "https://api.polygonscan.com/api",
            params={
                "module": "account",
                "action": "txlist",
                "address": address,
                "sort": "desc",
                "apikey": self.api_key or "YourApiKeyToken",
            },
            timeout=10.0,
        )
        resp.raise_for_status()
        return resp.json().get("result", [])
```

[CITED: finnhub_api.py lines 24-119]

### CLI Mode Registration

```python
# Source: cli.py (add to choices)

parser.add_argument(
    "--mode",
    choices=["all", "binary", ..., "logical-arb", "whale-copy"],
    default="all",
)
```

[CITED: cli.py lines 901-910]

### Continuous Mode Integration

```python
# Source: continuous.py (add to scan loop)

if args.mode in ("all", "logical-arb") and CONFIG_LOGICAL_ARB_ENABLED:
    try:
        from scans.logical_arb import scan_logical_arb
        from config import LOGICAL_ARB_RULES, LOGICAL_ARB_PRICE_THRESHOLD
        
        logical_arb_opps = scan_logical_arb(
            markets_by_key={},  # Populated from fetch_all_markets
            logical_arb_rules=LOGICAL_ARB_RULES,
            price_threshold=LOGICAL_ARB_PRICE_THRESHOLD,
        )
        all_opportunities.extend(logical_arb_opps)
    except Exception as e:
        logger.debug("Logical arb scan failed: %s", e)

if args.mode in ("all", "whale-copy") and CONFIG_WHALE_COPY_ENABLED:
    try:
        from scans.whale_copy import scan_whale_copy
        from config import WHALE_WALLETS, POLYGONSCAN_API_KEY
        
        polygonscan = PolygonscanClient(api_key=POLYGONSCAN_API_KEY)
        whale_copy_opps = scan_whale_copy(
            whale_wallets=WHALE_WALLETS,
            polygonscan_client=polygonscan,
        )
        all_opportunities.extend(whale_copy_opps)
    except Exception as e:
        logger.debug("Whale copy scan failed: %s", e)
```

[CITED: continuous.py lines 1163-1182 (imbalance), 1185-1207 (news-snipe) — identical pattern for Phase 9]

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Hard-coded cross-market rules | Config-driven JSON rules | Phase 9 (this phase) | Rule updates no longer require code deployment |
| Polling all Polymarket markets for whale activity | Targeted wallet monitoring via Polygonscan | Phase 9 (this phase) | Reduces API load 10x; latency improves from scan-all (30s) to target-wallets (10s) |
| Single-stage CLOB detection | Two-stage detection (mid-price candidate, CLOB refinement) | Phase 8 | False positive rate down 90%; execution pass rate up from 5% to 40%+ |
| Manual revalidation floors per strategy | Centralized REVAL_FLOORS in config.py | Phase 5 | Consistent risk management across 20+ strategies |

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | Polygonscan API free tier is 5 req/sec limit, same as other blockscans | Architecture Patterns > Pattern 2 | Phase 9 rate-limits aggressively but can still hit 429 if wallets list grows; may need Pro tier API key |
| A2 | Polymarket CLOB contract on Polygon is 0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e | Code Examples > API Client Pattern | Filtering transactions to wrong contract; whale copy produces zero results |
| A3 | Semantic rule JSON format `{"if_yes": "market_A", "then_yes": "market_B", "relationship": "implies"}` is sufficient | Architecture Patterns > Pattern 1 | Limited rule expressiveness; can't encode complex Boolean logic (AND, OR, NOT) |
| A4 | <30s latency budget for whale copy is achievable with sequential Polygonscan poll + CLOB revalidation + order placement | Architecture Patterns > Pattern 2 | Latency creep causes timeouts; need parallelization or async execution (not in Phase 9 scope) |
| A5 | Whale wallets remain profitable and active indefinitely | User Constraints > Locked Decisions | Wallet address list becomes stale; need periodic review and profitability tracking |

## Open Questions

1. **Semantic rule complexity:**
   - What we know: Rules are config-driven JSON with simple `implies` relationships.
   - What's unclear: Can rules encode negations (e.g., "NOT Bitcoin >$100k")? Can rules be chained (A→B→C)?
   - Recommendation: Start with `implies` only; add OR/AND operators in Phase 10 if needed.

2. **Whale wallet profitability tracking:**
   - What we know: WHALE_WALLETS list is user-provided.
   - What's unclear: How do we know which wallets are still profitable? Do we track P&L per wallet?
   - Recommendation: Dashboard includes "whale_pnl_per_wallet" leaderboard; user can remove wallets from list if P&L turns negative.

3. **CLOB transaction decoding:**
   - What we know: Whale copy polls Polygonscan for transactions to CLOB contract.
   - What's unclear: How do we parse transaction input (calldata) to extract trade direction and size?
   - Recommendation: Start with simplified version (just log the transaction hash); Phase 10 adds full calldata decoding via py-clob-client ABI.

4. **Logical arb rule validation:**
   - What we know: Rules are loaded from JSON at startup.
   - What's unclear: Do we validate rules for cycles, contradictions, or non-existent markets?
   - Recommendation: Add `config.validate_logical_arb_rules()` function that checks rule graph consistency.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| Polygonscan API (free tier) | Whale copy scanner | ✓ | Free + Pro | Skip whale copy if API key not set |
| Polymarket CLOB contract events | Whale copy + logical arb | ✓ | On-chain | Use py-clob-client REST API (slower) |
| Polymarket market data API | Both strategies | ✓ | Exists | Cache market data from previous scan |
| requests library | Polygonscan client | ✓ | 2.31.0 | (Already installed) |
| tenacity library | Retry backoff | ✓ | 9.1.4 | (Already installed) |

**Missing dependencies with no fallback:** None — all core dependencies exist in requirements.txt.

**Missing dependencies with fallback:**
- Polygonscan API key: Fallback to free tier (5 req/sec limit). If API timeouts, skip whale copy scan cycle and log warning.

## Validation Architecture

### Test Framework

| Property | Value |
|----------|-------|
| Framework | pytest (existing, Phase 1 baseline) |
| Config file | tests/conftest.py (to be updated with fixtures for Phase 9) |
| Quick run command | `pytest tests/test_logical_arb.py tests/test_whale_copy.py -v` |
| Full suite command | `pytest tests/ -v` |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| STRAT-04 | Logical arb detects semantic rule violations | unit | `pytest tests/test_logical_arb.py::TestScanStage1::test_detects_rule_violation -x` | ❌ Wave 0 |
| STRAT-04 | Logical arb Stage 2 refines with CLOB | unit | `pytest tests/test_logical_arb.py::TestRefinement -x` | ❌ Wave 0 |
| STRAT-04 | Logical arb opportunity dict has required keys | unit | `pytest tests/test_logical_arb.py::TestScanStage1::test_returns_required_keys -x` | ❌ Wave 0 |
| STRAT-05 | Whale copy polls Polygonscan successfully | unit | `pytest tests/test_whale_copy.py::TestPolygonscanFetch -x` | ❌ Wave 0 |
| STRAT-05 | Whale copy parses transaction calldata | unit | `pytest tests/test_whale_copy.py::TestTransactionParsing -x` | ❌ Wave 0 |
| STRAT-05 | Whale copy respects 5 concurrent position limit | integration | Manual — check dashboard positions endpoint | ❌ Wave 0 |
| Both | Strategies appear in dashboard P&L leaderboard | integration | Manual — verify /status endpoint includes strategy rows | ❌ Wave 0 |
| Both | Config flags LOGICAL_ARB_ENABLED, WHALE_COPY_ENABLED work | unit | `pytest tests/test_config.py -k "logical_arb or whale_copy" -x` | ✅ Exists |
| Both | CLI modes --mode logical-arb, --mode whale-copy registered | unit | `python scanner.py --help \| grep logical-arb` | ❌ Wave 0 |

### Sampling Rate

- **Per task commit:** `pytest tests/test_logical_arb.py tests/test_whale_copy.py -v --tb=short` (Phase 9 unit tests only)
- **Per wave merge:** `pytest tests/ -v` (full suite including existing Phase 1-8 tests)
- **Phase gate:** Full suite green + manual test of dashboard integration before `/gsd-verify-work`

### Wave 0 Gaps

- [ ] `tests/test_logical_arb.py` — Stage 1 scanning, Stage 2 refinement, edge cases (empty rules, malformed JSON)
- [ ] `tests/test_whale_copy.py` — Polygonscan API mocking, transaction parsing, rate limit simulation
- [ ] `tests/conftest.py` — Fixtures for mock Polygonscan responses, mock CLOB market data, mock wallet addresses
- [ ] `tests/test_config.py` — Add tests for LOGICAL_ARB_ENABLED, WHALE_COPY_ENABLED, LOGICAL_ARB_RULES parsing
- [ ] `tests/test_executor.py` — Add _build_legs branches for "LogicalArb" and "WhaleCopy" opportunity types
- [ ] `tests/test_dashboard.py` — Verify leaderboard rows for both strategies appear in /status endpoint

*(All gaps block execution until resolved. Framework install: pytest already exists from Phase 1.)*

## Security Domain

### Applicable ASVS Categories

| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | No | No user accounts in personal tool |
| V3 Session Management | No | No web sessions |
| V4 Access Control | No | No multi-user access |
| V5 Input Validation | Yes | Validate LOGICAL_ARB_RULES JSON schema at startup; validate WHALE_WALLETS as Polygon addresses (0x...) |
| V6 Cryptography | No | No encryption needed (all APIs use HTTPS; py-clob-client handles Ethereum signing) |
| V7 Error Handling | Yes | Don't expose Polygonscan API key in error messages; log sanitized errors |
| V8 Data Protection | No | No PII handled (wallet addresses are public on-chain) |
| V9 Communications | Yes | HTTPS only for Polygonscan and Polymarket APIs (tenacity + requests enforce) |

### Known Threat Patterns for Phase 9

| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| Invalid LOGICAL_ARB_RULES JSON crashes startup | Denial of Service | Validate JSON schema at config load; ConfigError on invalid syntax |
| Malicious LOGICAL_ARB_RULES creates circular trades | Tampering | Check rule graph for cycles (directed acyclic graph validation) |
| Polygonscan API key leaked in logs | Information Disclosure | Never log POLYGONSCAN_API_KEY; sanitize error messages |
| Replay attack on whale trades (execute stale wallet trades) | Replay | Check transaction timestamp; discard trades older than 30s |
| Rate limit exhaustion via high wallet list | Denial of Service | Implement client-side rate limiting (max 5 req/sec); log rate limit hits |
| Whale wallet address validation (phishing) | Spoofing | Validate WHALE_WALLETS against Polygonscan (all addresses 0x..., 40 hex chars) |

## Sources

### Primary (HIGH confidence)

- [Polymarket CLOB contract on Polygon](https://polygonscan.com/address/0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e) — confirmed via on-chain verification
- [Phase 8 scan architecture](scans/imbalance.py, scans/news_snipe.py, scans/correlated.py, scans/time_decay.py) — verified from codebase lines 1-201
- [Executor _build_legs pattern](executor.py:1135) — verified from codebase lines 1135-1550
- [Config feature flag pattern](config.py) — verified Phase 8 (IMBALANCE_ENABLED, NEWS_SNIPE_ENABLED, CORRELATED_ENABLED, TIME_DECAY_ENABLED)
- [Continuous mode integration](continuous.py:1163-1242) — verified Phase 8 scan handlers

### Secondary (MEDIUM confidence)

- [Polygonscan API free tier rate limits](https://polygonscan.com/apis) — 5 requests per second, 100K calls/day
- [Polymarket copy trading requirements (2026)](https://ericaai.tech.blog/2026/03/11/how-to-build-a-production-ready-polymarket-copy-trading-bot/) — confirms <30s latency requirement
- [Whale tracking on Polygon (2026)](https://www.quicknode.com/builders-guide/best/top-10-polymarket-whale-trackers) — confirms on-chain monitoring feasibility

### Tertiary (LOW confidence)

- [CLOB transaction parsing complexity](docs.polymarket.com not directly consulted) — assumed from py-clob-client usage; needs Phase 10 verification
- [Semantic rule JSON format](assumption based on CONTEXT.md) — not verified against external spec; user may need adjustment

## Metadata

**Confidence breakdown:**

- Standard stack: HIGH — all dependencies exist in requirements.txt; no new packages needed
- Architecture patterns: HIGH — Phase 8 provides proven two-stage scan and executor integration templates
- Polygonscan integration: MEDIUM — API docs exist but latency/reliability under load is untested in this codebase
- Semantic rule validation: LOW — no external validation library referenced; validation logic TBD in Phase 9 planning
- Dashboard integration: MEDIUM — existing leaderboard row pattern established but Phase 9 requirements not fully scoped

**Research date:** 2026-04-05  
**Valid until:** 2026-04-20 (15 days — Phase 9 is active dev; Polygonscan API stable but watch rate limit changes)

**Key decision points for planner:**

1. **CLOB transaction decoding:** Do we use simplified version (log hash only) or full calldata parsing (Phase 10)? Affects whale copy MVPscope.
2. **Semantic rule expressiveness:** Are `implies` relationships sufficient, or do we need AND/OR/NOT Boolean operators now?
3. **Wallet profitability tracking:** Should Phase 9 include P&L per wallet, or is manual list curation in Phase 10 acceptable?
4. **Rate limit strategy:** Should we upgrade to Polygonscan Pro tier immediately, or rely on free tier + exponential backoff?

---

All claims in this research tagged with sources. Ready for planning phase.
