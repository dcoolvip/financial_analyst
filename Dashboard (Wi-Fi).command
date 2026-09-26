#!/bin/zsh
# Like Dashboard.command, but reachable from phones/tablets on the same Wi-Fi.
# Password-protected. Close this window to stop it.
cd "${0:A:h}"
if [[ ! -x .venv/bin/streamlit ]]; then
  echo "First run: setting up (one time)..."
  python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt || exit 1
fi

if [[ ! -f data/app_password || "$1" == "--reset-password" ]]; then
  mkdir -p data
  echo "Choose a password for Wi-Fi access (used on your other devices):"
  read -s "pw?Password: "; echo
  read -s "pw2?Again: "; echo
  [[ "$pw" == "$pw2" && -n "$pw" ]] || { echo "Passwords didn't match."; exit 1; }
  PW="$pw" .venv/bin/python -c '
import hashlib, os
salt = os.urandom(16)
d = hashlib.scrypt(os.environ["PW"].encode(), salt=salt, n=2**14, r=8, p=1)
open("data/app_password", "w").write(salt.hex() + ":" + d.hex())'
  chmod 600 data/app_password
  echo "Saved. (Run this file with --reset-password to change it.)"
fi

IP=$(ipconfig getifaddr en0 || ipconfig getifaddr en1)
echo ""
echo "On your other device, open:  http://$IP:8501"
echo "If macOS asks whether Python may accept incoming connections, click Allow."
echo ""
FINANCE_LAN=1 .venv/bin/streamlit run app.py --server.port 8501 --server.address 0.0.0.0 &
sleep 2 && open http://localhost:8501
wait
