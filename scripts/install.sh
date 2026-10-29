#!/usr/bin/env bash
# Reproducible, system-wide install of the `hotel` CLI.
#   - Python pinned by .python-version (uv downloads it if missing)
#   - dependency versions pinned by constraints.txt (exported from uv.lock)
#   - installed as an isolated uv tool -> `hotel` on PATH (~/.local/bin)
# Usage: scripts/install.sh [lobby-dir]   (default lobby dir: ~/lobby)
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && (pwd -W 2>/dev/null || pwd))"  # pwd -W: native path on Git Bash/MSYS
py="$(cat "$here/.python-version")"
command -v uv >/dev/null || { echo "uv is required: https://docs.astral.sh/uv/"; exit 1; }
command -v git >/dev/null || { echo "git is required"; exit 1; }

uv tool install --force --python "$py" --constraints "$here/constraints.txt" "$here"
uv tool update-shell >/dev/null 2>&1 || true   # make sure ~/.local/bin is on PATH

home="${1:-$HOME/lobby}"
if [ ! -d "$home/.hotel" ]; then
  hotel init "$home"
fi
hotel home "$home" >/dev/null
hotel doctor || true
echo "done: run 'hotel' from any directory. The first open walks you through API keys and a short tour."
