# Binance.US account reporting

`binance_us_api.py` adds an opt-in balance reader for Binance.US. It makes only
`GET /api/v3/time` and signed `GET /api/v3/account` requests to
`https://api.binance.us`, refuses redirects, and exposes no order, cancellation,
withdrawal, conversion, transfer, or staking operation. It is not registered in
the scanner, executor, live launchers, or scheduled workers.

## Authentication

A Binance.US exchange API key and its matching HMAC secret are both required.
Use a read-enabled key with withdrawals disabled and an appropriate IP allowlist.
This reader does not require spot trading permission. Account flags in the output
are account capabilities, not API-key permissions or permission to place trades.

On macOS, the default source is login Keychain, with the current OS username as
the account and these services:

- `codex-binance-us-api-key`
- `codex-binance-us-secret-key`

```sh
python binance_us_api.py --source keychain
```

For Infisical, use the repository's existing project binding, environment `dev`,
and path `/`. Store `BINANCE_US_API_KEY` and `BINANCE_US_API_SECRET` through a
secure input path. Do not put values in command arguments, committed files,
chat, screenshots, or logs. Then run:

```sh
infisical run --env=dev --path=/ -- python binance_us_api.py --source env
```

The environment source requires both values and never silently mixes it with
Keychain. No dotenv files are loaded. Missing or malformed authentication stops
before any network request. CLI errors omit response bodies and signed URLs.
Output contains private balance information; keep it out of public artifacts.

An unauthenticated connectivity check is available independently:

```sh
python binance_us_api.py --public-check
```

This check proves only public API reachability. A complete secret pair, accepted
IP restriction and successful signed account response are still required to
verify account access. Saving a key alone does not establish authentication.

## Promotion decisions

Balances are not proof of enrollment, a qualifying first deposit, deposit
settlement, a completed holding period, or bonus credit. The account endpoint
does not expose SOL First Depositor Bonus eligibility. Resolve promotion rules
through the official terms and account support before acting on qualifying funds.
Do not infer a profit opportunity or trading authority from this connection.

## Implementation evidence

| Component | Version | Official source / retrieval | Contract | Validation |
| --- | --- | --- | --- | --- |
| HTTP client | Existing `requests==2.33.0`; no added dependency | [Binance.US REST docs](https://docs.binance.us/#get-user-account-information-user_data), 2026-10-01 | Account GET uses `X-MBX-APIKEY`, HMAC SHA256 over the encoded query, timestamp and receive window | Mocked signature, route, redirect, secret-source and malformed-response tests; signed account check requires the operator's complete key pair |
| Clock | REST v3 | [Server time](https://docs.binance.us/#get-server-time), 2026-10-01 | Public GET returns `serverTime` in milliseconds | Public check, with no authentication attached |

The official documentation capture used Firecrawl scrape
`01a0f84d-78d9-7791-80ce-035d9ed1b679` (provider-reported cost: 1 credit).

The module uses a 5-second receive window and exchange time, disables automatic
redirects, and does not retry failures or rate-limit responses. Nonzero free and
locked quantities retain their decimal strings; crypto is not valued as USD.
