#!/bin/zsh
# Double-click in Finder to open the dashboard. Close this window to stop it.
cd "${0:A:h}"
if [[ ! -x .venv/bin/streamlit ]]; then
  echo "First run: setting up (one time)..."
  python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt || exit 1
fi
.venv/bin/streamlit run app.py --server.port 8501 &
sleep 2 && open http://localhost:8501
wait
