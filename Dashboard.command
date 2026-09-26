#!/bin/zsh
# Double-click in Finder to open the dashboard (this Mac only). Close this window to stop it.
# Runs under the reloader: updates are picked up automatically - no need to reopen after changes.
cd "${0:A:h}"
source tools/env.sh || exit 1   # sets PY (venv outside iCloud) and DATA
trap 'kill $(jobs -p) 2>/dev/null' EXIT INT TERM
"$PY" -m finance.reloader --watch .streamlit/config.toml requirements.txt -- \
  "$PY" -m streamlit run app.py --server.port 8501 &
sleep 2 && open http://localhost:8501
wait
