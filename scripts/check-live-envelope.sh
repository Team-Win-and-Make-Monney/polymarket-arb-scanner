#!/bin/zsh
# Check-only live envelope gate. Never starts a runner, never places an order.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
ENVELOPE="${LIVE_ENVELOPE_PATH:-$REPO/artifacts/live-envelope.json}"
PY="${MM_D0_PYTHON:-python3}"
if [[ -x "$REPO/.venv/bin/python" ]]; then
  PY="$REPO/.venv/bin/python"
fi
export LIVE_ENVELOPE_PATH="$ENVELOPE"
if [[ ! -f "$ENVELOPE" ]]; then
  echo "[check] BLOCKED: live envelope missing at $ENVELOPE"
  echo "        Fill venue, pair, max_notional_usd, max_daily_loss_usd, kill_switch"
  echo "        from one [OP] message. Template: artifacts/live-envelope.example.json"
  exit 2
fi
"$PY" -c "from live_envelope import load_envelope; import json; print(json.dumps(load_envelope()))"
echo "[check] envelope valid — still not starting a live runner"
exit 0
