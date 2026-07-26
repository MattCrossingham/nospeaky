#!/usr/bin/env bash
# Start NoSpeaky engine on localhost only.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [[ ! -x .venv/bin/uvicorn ]]; then
  echo "Missing venv. Create with: python3 -m venv .venv && .venv/bin/pip install -r engine/requirements.txt"
  exit 1
fi
export NOSPEAKY_MODEL="${NOSPEAKY_MODEL:-mlx-community/whisper-small-mlx}"
exec .venv/bin/uvicorn engine.server:app --host 127.0.0.1 --port 8788 --app-dir "$ROOT"
