#!/bin/sh
# Permanent real-money live agent + account console on a single always-on
# Fly machine. The web console stays the MAIN process; the agent runs under a
# watchdog (auto-restarts on crash). Both share /app/state (the Fly volume),
# so credentials saved on the API-keys page are reused — nothing extra to
# configure. The agent writes its logs to /app/state/live/agent-stdout.log.
set -u

mkdir -p /app/state/live

CRED_DIR=/app/state/web/credentials
# The volume's meta.json is the validated credential the console uses — prefer it
# over any possibly-stale KALSHI_API_KEY_ID secret left over from an older key.
KEY_ID=""
if [ -f "$CRED_DIR/meta.json" ]; then
  KEY_ID=$(python3 -c "import json;print(json.load(open('$CRED_DIR/meta.json')).get('key_id',''))" 2>/dev/null || true)
fi
if [ -z "$KEY_ID" ]; then
  KEY_ID="${KALSHI_API_KEY_ID:-}"
fi

GROQ_ENV=""
if [ -f /app/state/groq_api_key ]; then
  GROQ_ENV=$(cat /app/state/groq_api_key)
  echo "fly-entrypoint: groq key found on volume — AI exits enabled"
else
  echo "fly-entrypoint: WARN no /app/state/groq_api_key — AI exit calls will fail" >&2
fi

if [ -n "$KEY_ID" ] && [ -f "$CRED_DIR/kalshi_private_key.pem" ]; then
  echo "fly-entrypoint: live agent ARMED (key ${KEY_ID})"
  (
    while true; do
      GROQ_API_KEY="$GROQ_ENV" \
      KALSHI_LIVE_CONFIRM=YES \
      KALSHI_API_KEY_ID="$KEY_ID" \
      KALSHI_PRIVATE_KEY_PATH="$CRED_DIR/kalshi_private_key.pem" \
      python3 -m kalshi_agent.cli --config config-live.yaml live --live \
        >> /app/state/live/agent-stdout.log 2>&1
      rc=$?
      echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) live agent exited rc=$rc — restarting in 5s" >> /app/state/live/agent-stdout.log
      sleep 5
    done
  ) &
else
  echo "fly-entrypoint: credentials missing on volume — live agent DISABLED (console only)" >&2
fi

exec python3 -m kalshi_agent.cli web --port "${PORT:-8080}" --mode account --no-autostart
