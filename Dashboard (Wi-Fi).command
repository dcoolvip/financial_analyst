#!/bin/zsh
# Like Dashboard.command, but reachable from phones/tablets on the same Wi-Fi.
# Password-protected. Close this window to stop it.
cd "${0:A:h}"
if [[ ! -x .venv/bin/streamlit ]]; then
  echo "First run: setting up (one time)..."
  python3 -m venv .venv || exit 1
fi
# Install/refresh packages whenever requirements.txt has changed since the last launch
if [[ ! -f .venv/.req-stamp || requirements.txt -nt .venv/.req-stamp ]]; then
  .venv/bin/pip install -q -r requirements.txt && touch .venv/.req-stamp
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
[[ -n "$IP" ]] || { echo "Not connected to Wi-Fi."; exit 1; }
HOST=dcool.home

# HTTPS only: create/renew certificates (no-op when they're current)
tools/make_tls.sh "$HOST" || exit 1
if [[ ! -f data/tls/.phone-setup-shown ]]; then
  cat <<EOF

──────────────── One-time iPhone setup (so it trusts this dashboard) ────────────────
 1. AirDrop "Financial Analyst CA.cer" (Finder is showing it now) to your iPhone.
 2. iPhone: Settings → General → VPN & Device Management → the downloaded profile → Install.
 3. iPhone: Settings → General → About → Certificate Trust Settings →
    turn ON "Financial Analyst local CA (dcool.home)".
 This certificate can only vouch for $HOST and your home network - nothing else.
──────────────────────────────────────────────────────────────────────────────────────
EOF
  open -R "data/tls/Financial Analyst CA.cer"
  touch data/tls/.phone-setup-shown
fi

echo ""
echo "Open on any device on this Wi-Fi:  https://$HOST:8501"
echo "If macOS asks whether Python may accept incoming connections, click Allow."
echo "Add Face ID: sign in once, then sidebar → 🔐 Security & passkeys → Add a passkey."
echo ""
# Streamlit: this Mac only (127.0.0.1). The gate: HTTPS on the Wi-Fi address only, signs people in,
# and is the only way in from the network.
trap 'kill $(jobs -p) 2>/dev/null' EXIT INT TERM
# Both run under the reloader (like Flask's): code/cert/package changes are picked up automatically and
# a crash is retried - no need to close this window after updates. Streamlit also hot-reloads app code itself.
PY=.venv/bin/python
FINANCE_GATE=1 $PY -m finance.reloader --watch .streamlit/config.toml -- \
  $PY -m streamlit run app.py --server.port 8502 --server.address 127.0.0.1 &
$PY -m finance.reloader --watch finance/gate.py finance/auth_store.py requirements.txt \
    data/tls/server.pem data/tls/server.key -- \
  $PY -m finance.gate --host "$HOST" --ip "$IP" --port 8501 --upstream-port 8502 &
sleep 3 && open "https://$HOST:8501"
wait
