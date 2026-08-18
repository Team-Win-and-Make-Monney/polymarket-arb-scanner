# Kalshi D0 live evidence package — 2026-08-17/18

**Authority:** live target, no live order. Envelope missing.

Head: `feat/kalshi-factory-2026-08-17` @ `56c872c`. PR #131 Tests were green on the CI-fixture commit; review follow-ups pushed.

## Fail-closed checks (this session)

| Check | Result |
|---|---|
| `artifacts/live-envelope.json` present | No |
| `python3 -c "from live_envelope import load_envelope; load_envelope()"` | `EnvelopeError` missing file |
| `scripts/check-live-envelope.sh` | Exit 2 |
| `scripts/launch-kalshi-d0-live.sh` | Must not be started without envelope |
| `DRY_RUN=false` import of `config` | `ConfigError` without envelope |
| Order path without envelope | `live_envelope_missing` |
| Order path with `pair=KXFED` on `KXTEST-…` | `envelope_pair` |
| Sports / mention selection | Blocked in `kalshi_policy.event_blocked` and `lip_select` |
| International Polymarket scan | Skipped when `SCAN_VENUES=kalshi` |

## What starts live

Operator sends one message with all five fields. Then:

1. Write `artifacts/live-envelope.json` from that message (do not invent numbers).
2. `scripts/check-live-envelope.sh` must print the parsed envelope.
3. `scripts/launch-kalshi-d0-live.sh` starts `scanner.py --continuous --mode mm-pilot` with `DRY_RUN=false`, hedge + toxic + vol on, Kalshi-only.

## Measurement already running (no orders)

- Reward-share logger: public API, first live sample 18 Aug 03:27 UTC, 8 books, median share 0.055.
- Daily GitHub workflow: `.github/workflows/kalshi-reward-share.yml`
- Structural detectors: 2 complete-set underprice flags on 80 events.

## Slack / Railway health

`health-monitor.yml` still pages only if `MONITOR_SLACK_WEBHOOK` is set in GitHub secrets. That secret was historically absent. Setting it is an operator GitHub action, not an agent-invented live order.

## Tax

Counsel brief: command-center `04-working-notes/tax-character-counsel-brief-2026-08-17.md`. Opinion not purchased.
