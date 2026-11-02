#!/usr/bin/env bash
# Install Restaurant Ops Agents on a Linux or macOS host without Docker.
#   scripts/install.sh            # install deps, build the UI, seed the demo data, run the backup drill
#   scripts/install.sh --service  # also write a systemd unit (Linux, needs sudo) that starts it on boot
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing: $1 ($2)"; exit 1; }; }
need uv "https://docs.astral.sh/uv/getting-started/installation/"
need node "Node.js 18+ for the UI build"
need npm "comes with Node.js"

echo "==> Python dependencies"
uv sync --frozen
echo "==> UI build"
(cd frontend && npm ci --no-audit --no-fund && npm run build)
[ -f .env ] || { [ -f .env.example ] && cp .env.example .env && echo "==> wrote .env from .env.example (edit it)"; }
echo "==> demo database + forecast models (first run only)"
[ -f data/restaurant.db ] || uv run python -m restaurant_ops.data.seed
echo "==> backup / restore drill"
uv run python -m restaurant_ops.live.ops drill
command -v redis-server >/dev/null 2>&1 || echo "note: redis-server not found; the event bus will run in-process (fine for one host)"

if [ "${1:-}" = "--service" ]; then
  UNIT=/etc/systemd/system/restaurant-ops.service
  sudo tee "$UNIT" >/dev/null <<UNIT
[Unit]
Description=Restaurant Ops Agents
After=network-online.target redis-server.service
[Service]
WorkingDirectory=$ROOT
ExecStart=$(command -v uv) run --frozen python -m restaurant_ops.live.server
Restart=on-failure
User=$(id -un)
[Install]
WantedBy=multi-user.target
UNIT
  sudo systemctl daemon-reload && sudo systemctl enable --now restaurant-ops
  echo "==> systemd unit installed: sudo systemctl status restaurant-ops"
fi
echo "Done. Start with: uv run python -m restaurant_ops.live.server   (http://127.0.0.1:8000)"
[ -f data/FIRST_RUN_ADMIN.txt ] && echo "First admin password: data/FIRST_RUN_ADMIN.txt (created on first start)" || true
