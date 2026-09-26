#!/bin/zsh
# Double-click in Finder to open the dashboard (this Mac only). Close this window to stop it.
# Runs under the reloader: updates are picked up automatically - no need to reopen after changes.
cd "${0:A:h}"
if [[ ! -x .venv/bin/streamlit ]]; then
  echo "First run: setting up (one time)..."
  python3 -m venv .venv || exit 1
fi
# Install/refresh packages whenever requirements.txt has changed since the last launch
if [[ ! -f .venv/.req-stamp || requirements.txt -nt .venv/.req-stamp ]]; then
  .venv/bin/pip install -q -r requirements.txt && touch .venv/.req-stamp
fi
trap 'kill $(jobs -p) 2>/dev/null' EXIT INT TERM
PY=.venv/bin/python
$PY -m finance.reloader --watch .streamlit/config.toml requirements.txt -- \
  $PY -m streamlit run app.py --server.port 8501 &
sleep 2 && open http://localhost:8501
wait
