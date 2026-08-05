# Platform Integration Matrix

> **Owner:** Jonathon Tamm · **Review cadence:** monthly, or whenever a platform client/auth/fee changes.
> **Canonical source of truth** for platform capability, auth, fees, and integration status.
> CLAUDE.md and `docs/strategy-framework-v2.md` derive their platform claims from this file — update here first.

## Capability matrix

| Platform | Role | Auth | Read | Buy | Sell | Streaming | Fee model | Feature flag | Status |
|---|---|---|---|---|---|---|---|---|---|
| Polymarket | Trade | Ethereum private key → CLOB (`py-clob-client`) | ✅ | ✅ | ✅ | ✅ WS | Gas (Polygon) + 0 trading fee on most markets | — (core) | BUILT |
| Kalshi | Trade | RSA-PSS signed headers (key file or base64) | ✅ | ✅ | ✅ | ✅ WS | Built into pricing (~≤2% of max profit) | — (core) | BUILT |
| Betfair | Trade | SSO login + API key | ✅ | ✅ | ✅ | ❌ | Commission (`BETFAIR_COMMISSION_RATE`) | — | BUILT |
| Smarkets | Trade | API key session | ✅ | ✅ | ✅ | ❌ | Commission (`SMARKETS_COMMISSION_RATE`) | — | BUILT |
| SX Bet | Trade | API key session | ✅ | ⚠️ | ⚠️ | ❌ | Exchange fee | — | **PARTIAL — read-only**: `place_order()` sends unsigned JSON; EIP-712 signing not implemented. `validate_config()` errors at startup if `sxbet` ∈ `ENABLED_EXECUTION_PLATFORMS` while `DRY_RUN=false` |
| Matchbook | Trade | Username/password session | ✅ | ✅ | ✅ | ❌ | 0% commission on predictions | — | BUILT |
| Gemini Predictions | Trade | HMAC-SHA384 (API key + secret) | ✅ | ✅ | ✅ | ❌ | **1.75% maker / 7% taker**, `roundup(rate×C×P×(1−P))` (CFTC 40.6 filing eff. 2026-03-09) | — | BUILT |
| IBKR ForecastEx | Trade | TWS API via `ib_insync` (IB Gateway socket) | ✅ | ✅ | ❌ | ❌ | $0.00 commission | — | **BUILT — BUY-only, LMT-only**, 5s order rate limit |
| Metaculus | Signal | Current `/api/posts` REST (`METACULUS_API_KEY` + approved commercial access) | ✅ | — | — | ❌ | n/a | `EVENT_MONITOR_ENABLED` + `METACULUS_COMMERCIAL_USE_APPROVED` | BUILT (read-only; disabled unless both access gates are explicit) |
| Manifold | Signal | Public REST | ✅ | — | — | ❌ | n/a | `EVENT_MONITOR_ENABLED` | BUILT (read-only) |

## Authz / custody / access (security review columns)

| Platform | API-key scope | Trade vs. withdraw separation | Signing model | Key rotation | IP / geo restriction | ToS on automated/API trading |
|---|---|---|---|---|---|---|
| Polymarket | Wallet private key = full control (trade **and** transfer) | ❌ none — same key signs trades and USDC withdrawals (auto-rebalance corridor) | EIP-712 order signing | Manual (rotate wallet) | Geo-restricted in several US states | Permitted via CLOB API |
| Kalshi | API key scoped to account trading | Withdrawals via separate web flow (not API) | RSA-PSS request signing | Manual key regeneration | US-regulated; state-by-state | Permitted |
| Betfair | App key + session | Read-only balance; transfers off-API | Session token | Manual | UK/EU; geo-gated | Permitted (API tier) |
| Smarkets | API key | Read-only balance | Session | Manual | UK/EU | Permitted |
| SX Bet | API key | n/a (trading blocked) | **EIP-712 missing** (gap) | Manual | Crypto-native | Permitted |
| Matchbook | User/pass session | Read-only balance | Session | Manual (password) | UK/EU | Permitted |
| Gemini | API key + secret; **master keys (`master-` prefix) require `"account":"primary"`** in every payload | ❌ trade + withdraw share key scope (used by auto-rebalance) | HMAC-SHA384 | Manual via Gemini console | US-regulated | Permitted (Predictions API) |
| IBKR | IB Gateway socket session (`IBKR_CLIENT_ID`) | Transfers off-API | Gateway-authenticated | Gateway re-auth | Requires reachable Gateway host | Permitted (TWS API) |
| Metaculus / Manifold | Read-only; no trading scope | n/a | n/a | n/a | None material | Read permitted |

**Custody note:** the only programmatic fund movement is the **Gemini ↔ Polymarket USDC-on-Polygon auto-rebalance corridor** (`AUTO_REBALANCE_ENABLED`, default off). The other six trading platforms expose **no** withdraw/transfer API and stay on the manual-rebalance path with weekly digests. Because Polymarket and Gemini keys are not trade/withdraw-separated, treat those two secrets as custody-grade (see `SECURITY.md`).

## Candidate platforms (not yet integrated)
See `docs/audit/PLATFORM-RESEARCH-2026-05-31.md` for the ranked expansion memo (Tier 1: Sporttrade/Novig/ProphetX; Tier 2: Predict.fun/Myriad/Limitless; Tier 3: Drift, Crypto.com/OG). Each is gated on regulatory eligibility **and** operational-readiness before greenlight.
