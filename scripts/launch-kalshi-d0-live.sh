#!/bin/zsh
# Launch the Kalshi Reward-MM D0 LIVE runner.
# Fail-closed: refuses to start unless artifacts/live-envelope.json contains
# all five operator fields from one message (venue, pair, max notional,
# max daily loss, kill switch). This script does not invent those limits.
set -euo pipefail

REPO="${MM_D0_REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
PY="${MM_D0_PYTHON:-$HOME/Dev/polymarket-arb-scanner/.venv/bin/python}"
STATUS_URL="https://api.elections.kalshi.com/trade-api/v2/exchange/status"
ENVELOPE="${LIVE_ENVELOPE_PATH:-$REPO/artifacts/live-envelope.json}"

cd "$REPO"

if [[ ! -f "$ENVELOPE" ]]; then
  echo "[live] BLOCKED: no envelope at $ENVELOPE" >&2
  echo "       Copy artifacts/live-envelope.example.json and fill venue, pair," >&2
  echo "       max_notional_usd, max_daily_loss_usd, kill_switch from one [OP] message." >&2
  exit 2
fi

if ! "$PY" -c "
from live_envelope import load_envelope
import json, os, sys
os.environ['LIVE_ENVELOPE_PATH'] = sys.argv[1]
envelope = load_envelope()
if envelope.get('venue') != 'kalshi-d0':
    raise SystemExit('launcher is Kalshi D0 only')
print(json.dumps(envelope))
" "$ENVELOPE"; then
  echo "[live] BLOCKED: envelope failed validation" >&2
  exit 2
fi

if [[ "${PREFLIGHT_ONLY:-}" == "1" ]]; then
  echo "[live] envelope valid; PREFLIGHT_ONLY=1 — not starting a runner"
  exit 0
fi

PROJECT_ID=$(jq -r .workspaceId .infisical.json)
INFISICAL_ARGS=(--projectId "$PROJECT_ID" --env=dev)
CLIENT_ID=$(security find-generic-password -a infisical -s mm-d0-client-id -w 2>/dev/null || true)
CLIENT_SECRET=$(security find-generic-password -a infisical -s mm-d0-client-secret -w 2>/dev/null || true)
if [[ -n "$CLIENT_ID" && -n "$CLIENT_SECRET" ]]; then
  INFISICAL_TOKEN=$(infisical login --method=universal-auth \
    --client-id="$CLIENT_ID" --client-secret="$CLIENT_SECRET" --silent --plain)
  export INFISICAL_TOKEN
fi
if ! infisical run "${INFISICAL_ARGS[@]}" -- true >/dev/null 2>&1; then
  echo "[live] FAIL: Infisical auth unusable" >&2
  exit 1
fi

until curl -sS -m 10 "$STATUS_URL" | grep -Eq '"exchange_active"[[:space:]]*:[[:space:]]*true'; do
  echo "[live] Kalshi exchange inactive — retry in 300s"
  sleep 300
done

infisical run "${INFISICAL_ARGS[@]}" -- "$PY" -c "import config" >/dev/null

TS=$(date -u +%Y%m%dT%H%M%SZ)
DIR="$REPO/artifacts/mm-d0-live/$TS"
mkdir -p "$DIR"
COMMIT=$(git rev-parse HEAD)
cat > "$DIR/RUN.md" <<EOF
# Kalshi Reward-MM D0 LIVE

- Start (UTC): $(date -u +%Y-%m-%dT%H:%M:%SZ)
- Commit: \`$COMMIT\`
- Envelope: \`$ENVELOPE\`
- Boundary: DRY_RUN=false, EXECUTION_MODE=full-auto, ENABLED_EXECUTION_PLATFORMS=kalshi, REQUIRE_KALSHI=true, PAPER_SCAN_VENUES=kalshi
- Kill: screen -S mm-d0-live-$TS -X quit
EOF

export MM_D0_RUN_DIR="$DIR" MM_D0_PROJECT_ID="$PROJECT_ID" MM_D0_PY_BIN="$PY" MM_D0_REPO_DIR="$REPO" LIVE_ENVELOPE_PATH="$ENVELOPE"
screen -dmS "mm-d0-live-$TS" zsh -c 'cd "$MM_D0_REPO_DIR" && \
  DRY_RUN=false EXECUTION_MODE=full-auto ENABLED_EXECUTION_PLATFORMS=kalshi \
  SCAN_VENUES=kalshi PAPER_SCAN_VENUES=kalshi MM_KALSHI_PILOT_ENABLED=true REQUIRE_KALSHI=true \
  MM_AUTO_HEDGE_ENABLED=true MM_TOXIC_FLOW_ENABLED=true MM_VOLATILITY_ADJUSTED_ENABLED=true \
  LIVE_ENVELOPE_PATH="$LIVE_ENVELOPE_PATH" \
  LOG_FILE="$MM_D0_RUN_DIR/scanner.log" MM_STATE_PATH="$MM_D0_RUN_DIR/mm_pilot_state.json" \
  MM_DECISIONS_LOG_PATH="$MM_D0_RUN_DIR/decisions.jsonl" \
  infisical run --projectId "$MM_D0_PROJECT_ID" --env=dev -- \
  "$MM_D0_PY_BIN" scanner.py --continuous --mode mm-pilot >> "$MM_D0_RUN_DIR/process.out" 2>&1'
echo "[live] mm-d0-live-$TS started — verifying boot (120s)"
sleep 120
if grep -q "Kalshi authenticated successfully" "$DIR/process.out" \
   && grep -q "Kalshi MM pilot started" "$DIR/process.out"; then
  echo "[verify] LIVE RUNNER UP: mm-d0-live-$TS"
  echo "[verify] evidence: $DIR"
  exit 0
fi
echo "[verify] FAIL — live boot did not reach authenticated MM pilot; stopping" >&2
screen -S "mm-d0-live-$TS" -X quit 2>/dev/null || true
echo "VOID — live boot verification failed" >> "$DIR/RUN.md"
tail -20 "$DIR/process.out" >&2
exit 1
