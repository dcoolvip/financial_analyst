# Sourced by the launchers and tools: sets up and exports PY (the project's Python) and DATA.
#
# The virtualenv lives OUTSIDE ~/Documents (iCloud-synced): venvs contain absolute paths and
# machine-specific binaries, and iCloud syncing them between Macs corrupts them.
# Override the location with FINANCE_VENV.
VENV="${FINANCE_VENV:-$HOME/.venvs/financial-analyst}"
PY="$VENV/bin/python"

if [[ ! -x "$PY" ]]; then
  echo "First run: setting up Python environment in $VENV (one time)..."
  mkdir -p "${VENV:h}" && python3 -m venv "$VENV" || return 1
fi
# Install/refresh packages whenever requirements.txt changed since the last launch
if [[ ! -f "$VENV/.req-stamp" || requirements.txt -nt "$VENV/.req-stamp" ]]; then
  "$PY" -m pip install -q -r requirements.txt && touch "$VENV/.req-stamp"
fi
# Remove the old in-repo .venv (pre-move) once nothing is running from it.
# pgrep exits 1 only for a definite "no match" - any other outcome keeps the folder.
if [[ -d .venv ]]; then
  pgrep -f "\.venv/bin/python" >/dev/null; in_use=$?
  if (( in_use == 1 )); then
    rm -rf .venv && echo "Removed the old .venv from the project folder (now in $VENV)."
  fi
fi
DATA=$("$PY" -c "from finance.paths import data_dir; print(data_dir())")
export PY DATA
