#!/usr/bin/env bash
# One-command launcher: starts the bot AND the Streamlit monitor together.
#
#   ./scripts/run_all.sh            # uses BOT_MODE from .env (default: mock)
#   ./scripts/run_all.sh mock       # force offline mock mode
#   ./scripts/run_all.sh testnet    # force live paper (demo/testnet) trading
#
# The bot runs in the background (logs -> runtime/bot.log); Streamlit runs in
# the foreground. Press Ctrl-C once to stop BOTH cleanly.
set -euo pipefail

# repo root = parent of this script's dir, regardless of where it's called from
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# optional first arg overrides BOT_MODE for this run
if [ "${1:-}" != "" ]; then
  export BOT_MODE="$1"
fi

# mock mode normally fires 300 ticks instantly and exits — useless for a live
# dashboard. When launching mock, run it CONTINUOUSLY at a watchable pace so the
# monitor shows moving data. (Override by exporting these yourself first.)
if [ "${BOT_MODE:-mock}" = "mock" ]; then
  : "${MOCK_TICKS:=100000}"
  : "${MOCK_TICK_SLEEP:=0.5}"
  export MOCK_TICKS MOCK_TICK_SLEEP
fi

mkdir -p runtime
BOT_LOG="runtime/bot.log"

echo "[run_all] starting bot (BOT_MODE=${BOT_MODE:-from .env}) -> $BOT_LOG"
python main.py > "$BOT_LOG" 2>&1 &
BOT_PID=$!

# make sure the bot dies when this script exits (Ctrl-C, error, or normal exit)
cleanup() {
  echo ""
  echo "[run_all] stopping bot (pid $BOT_PID)…"
  kill "$BOT_PID" 2>/dev/null || true
  wait "$BOT_PID" 2>/dev/null || true
  echo "[run_all] done."
}
trap cleanup EXIT INT TERM

# give the bot a moment; bail out early if it crashed on startup
sleep 2
if ! kill -0 "$BOT_PID" 2>/dev/null; then
  echo "[run_all] ERROR: bot exited immediately. Last log lines:" >&2
  tail -n 20 "$BOT_LOG" >&2
  exit 1
fi
echo "[run_all] bot is up. tailing its log:  tail -f $BOT_LOG"

# skip Streamlit's first-run interactive "Email:" prompt (only if not set up yet)
if [ ! -f "$HOME/.streamlit/credentials.toml" ]; then
  mkdir -p "$HOME/.streamlit"
  printf '[general]\nemail = ""\n' > "$HOME/.streamlit/credentials.toml"
fi

echo "[run_all] launching Streamlit monitor (Ctrl-C to stop everything)…"
streamlit run src/part7_dashboard/app.py --browser.gatherUsageStats false
