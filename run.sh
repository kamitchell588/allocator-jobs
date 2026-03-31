#!/usr/bin/env bash
# ── Institutional Allocator Job Scraper ──────────────────────────────────────
# Run with:  bash run.sh
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
PYTHON="$VENV_DIR/bin/python"
PIP="$VENV_DIR/bin/pip"

echo "============================================================"
echo "  Institutional Allocator Job Scraper"
echo "============================================================"

# 1. Create virtual environment if missing
if [ ! -d "$VENV_DIR" ]; then
  echo "Creating virtual environment at $VENV_DIR …"
  python3 -m venv "$VENV_DIR"
fi

# 2. Install / upgrade requirements
echo "Installing requirements …"
"$PIP" install -q --upgrade pip
"$PIP" install -q -r "$SCRIPT_DIR/requirements.txt"

# 3. Run the scraper
echo ""
"$PYTHON" "$SCRIPT_DIR/scraper.py"

echo ""
echo "Output files:"
echo "  CSV       : $SCRIPT_DIR/allocator_jobs.csv"
echo "  Dashboard : $SCRIPT_DIR/dashboard.html"
echo ""
echo "Open the dashboard in your browser:"
echo "  open \"$SCRIPT_DIR/dashboard.html\""
