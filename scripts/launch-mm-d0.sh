#!/bin/zsh
# Launch the Kalshi Reward-MM D0 dry-run soak with the full pre-flight that the
# 2026-07-23 incidents made mandatory:
#   06:54Z boot died at an interactive Infisical login prompt (expired session)
#   08:06Z boot degraded to Polymarket-only inside Kalshi's maintenance window
# Pre-flight order: secrets auth (machine identity preferred) -> exchange open
# -> deps import -> launch under screen with REQUIRE_KALSHI=true -> boot verify.
#
# Operator setup for headless auth (one-time):
#   Infisical dashboard -> Org Access Control -> Identities -> create
#   `mm-d0-runner` with Universal Auth, grant read on this project's `dev` env,
#   then store the credentials in Keychain (never in files or chat):
#     security add-generic-password -a infisical -s mm-d0-client-id -w '<ID>'
#     security add-generic-password -a infisical -s mm-d0-client-secret -w '<SECRET>'
# Without those Keychain entries the script falls back to the personal
# `infisical login` session and fails loudly if that session is expired.
set -euo pipefail

REPO="${MM_D0_REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
PY="${MM_D0_PYTHON:-$HOME/Dev/polymarket-arb-scanner/.venv/bin/python}"
STATUS_URL="https://api.elections.kalshi.com/trade-api/v2/exchange/status"

cd "$REPO"

# Machine-identity tokens can't infer the project from .infisical.json —
# `infisical run` errors with "Project ID is required" (verified 2026-07-23).
# Pass it explicitly; harmless for personal-session auth too.
PROJECT_ID=$(jq -r .workspaceId .infisical.json)
INFISICAL_ARGS=(--projectId "$PROJECT_ID" --env=dev)

# --- 1. Secrets auth: machine identity if provisioned, else personal session
CLIENT_ID=$(security find-generic-password -a infisical -s mm-d0-client-id -w 2>/dev/null || true)
CLIENT_SECRET=$(security find-generic-password -a infisical -s mm-d0-client-secret -w 2>/dev/null || true)
if [[ -n "$CLIENT_ID" && -n "$CLIENT_SECRET" ]]; then
  echo "[preflight] using Infisical machine identity"
  INFISICAL_TOKEN=$(infisical login --method=universal-auth \
    --client-id="$CLIENT_ID" --client-secret="$CLIENT_SECRET" --silent --plain)
  export INFISICAL_TOKEN
else
  echo "[preflight] no machine identity in Keychain — falling back to personal session"
fi
if ! infisical run "${INFISICAL_ARGS[@]}" -- true >/dev/null 2>&1; then
  echo "[preflight] FAIL: Infisical auth unusable (session expired and no machine identity)." >&2
  echo "           Fix: provision the machine identity (header comment) or run: infisical login" >&2
  exit 1
fi

# --- 2. Exchange must be open — a soak started in the maintenance window voids itself
# Whitespace-tolerant match: the status JSON's formatting is not contractual.
until curl -sS -m 10 "$STATUS_URL" | grep -Eq '"exchange_active"[[:space:]]*:[[:space:]]*true'; do
  echo "[preflight] Kalshi exchange inactive (maintenance window?) — retry in 300s"
  sleep 300
done

# --- 3. Deps import
infisical run "${INFISICAL_ARGS[@]}" -- "$PY" -c "import config" >/dev/null

# --- 4. Launch under screen with the approved D0 boundary
TS=$(date -u +%Y%m%dT%H%M%SZ)
DIR="$REPO/artifacts/mm-d0/$TS"
mkdir -p "$DIR"
COMMIT=$(git rev-parse HEAD)
cat > "$DIR/RUN.md" <<EOF
# Kalshi Reward-MM D0 Dry-Run Soak

- Start (UTC): $(date -u +%Y-%m-%dT%H:%M:%SZ)
- Earliest valid stop (UTC): $(date -u -v+48H +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d '+48 hours' +%Y-%m-%dT%H:%M:%SZ)
- Commit: \`$COMMIT\`
- Screen session: \`mm-d0-$TS\`
- Execution boundary: \`DRY_RUN=true\`, \`EXECUTION_MODE=semi-auto\`, \`ENABLED_EXECUTION_PLATFORMS=kalshi\`, \`REQUIRE_KALSHI=true\`
- Pilot boundary: Kalshi LIP only; Polymarket execution disabled; no canary approval
- Evidence: \`scanner.log\`, \`process.out\`, \`decisions.jsonl\`, \`mm_pilot_state.json\`
- Launched by: scripts/launch-mm-d0.sh (pre-flight: secrets auth, exchange open, imports)

The 48-hour gate is not passed until the process remains uninterrupted through
the earliest valid stop, exits cleanly on SIGTERM, and the final evidence is
reviewed. No canary or live-order authority is implied by this run.
EOF

# INFISICAL_TOKEN (exported in step 1) reaches the child via environment
# inheritance — it must NEVER be interpolated into the command line, where
# it would sit visible in the process list for the soak's whole 48 hours.
# Per-run paths ride the environment the same way. `--mode mm-pilot` is
# required: since #98–#108 the MM pilot only starts in that mode.
export MM_D0_RUN_DIR="$DIR" MM_D0_PROJECT_ID="$PROJECT_ID" MM_D0_PY_BIN="$PY" MM_D0_REPO_DIR="$REPO"
screen -dmS "mm-d0-$TS" zsh -c 'cd "$MM_D0_REPO_DIR" && \
  DRY_RUN=true EXECUTION_MODE=semi-auto ENABLED_EXECUTION_PLATFORMS=kalshi \
  MM_KALSHI_PILOT_ENABLED=true REQUIRE_KALSHI=true \
  LOG_FILE="$MM_D0_RUN_DIR/scanner.log" MM_STATE_PATH="$MM_D0_RUN_DIR/mm_pilot_state.json" \
  MM_DECISIONS_LOG_PATH="$MM_D0_RUN_DIR/decisions.jsonl" \
  infisical run --projectId "$MM_D0_PROJECT_ID" --env=dev -- \
  "$MM_D0_PY_BIN" scanner.py --continuous --mode mm-pilot >> "$MM_D0_RUN_DIR/process.out" 2>&1'
echo "[launch] mm-d0-$TS started — verifying boot (120s)"

# --- 5. Boot verification: Kalshi authenticated AND the MM pilot actually started
sleep 120
if grep -q "Kalshi authenticated successfully" "$DIR/process.out" \
   && grep -q "Kalshi MM pilot started" "$DIR/process.out"; then
  echo "[verify] SOAK LIVE: mm-d0-$TS"
  echo "[verify] evidence: $DIR"
  grep -m1 "Mode: CONTINUOUS" "$DIR/process.out" || true
else
  echo "[verify] FAIL — boot did not reach an authenticated MM pilot; stopping run" >&2
  screen -S "mm-d0-$TS" -X quit 2>/dev/null || true
  echo "VOID — boot verification failed (launch-mm-d0.sh)" >> "$DIR/RUN.md"
  tail -20 "$DIR/process.out" >&2
  exit 1
fi
