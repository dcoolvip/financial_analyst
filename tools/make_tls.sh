#!/bin/zsh
# Creates the HTTPS certificates for Wi-Fi mode, in data/tls/ (gitignored).
#
#   ca.key / ca.pem       Your private certificate authority. Name-constrained: it can only vouch for
#                         dcool.home, localhost and your home subnet - so even if ca.key leaked it could
#                         not impersonate your bank or any other real site.
#   Financial Analyst CA.cer   The same CA in the format iPhone wants. AirDrop it to the phone once.
#   server.key / server.pem    The dashboard's certificate, signed by the CA. Valid 397 days
#                              (Apple's limit); renewed automatically - no need to re-trust the phone.
#
# Usage: tools/make_tls.sh [hostname]      (default dcool.home). Safe to run repeatedly.
set -euo pipefail
cd "${0:A:h}/.."

HOST=${1:-dcool.home}
IP=$(ipconfig getifaddr en0 || ipconfig getifaddr en1 || true)
[[ -n "$IP" ]] || { echo "Not on Wi-Fi; can't determine this Mac's address." >&2; exit 1; }
SUBNET=${IP%.*}.0
D=data/tls
mkdir -p $D && chmod 700 $D
umask 077

if [[ ! -f $D/ca.key ]]; then
  echo "Creating your private certificate authority..."
  cat > $D/ca.ext <<EOF
basicConstraints=critical,CA:TRUE,pathlen:0
keyUsage=critical,keyCertSign,cRLSign
subjectKeyIdentifier=hash
nameConstraints=critical,permitted;DNS:$HOST,permitted;DNS:localhost,permitted;IP:$SUBNET/255.255.255.0,permitted;IP:127.0.0.1/255.255.255.255
EOF
  openssl ecparam -name prime256v1 -genkey -noout -out $D/ca.key
  openssl req -new -key $D/ca.key -subj "/CN=Financial Analyst local CA ($HOST)" -out $D/ca.csr
  openssl x509 -req -in $D/ca.csr -signkey $D/ca.key -days 3650 -sha256 -extfile $D/ca.ext -out $D/ca.pem
  openssl x509 -in $D/ca.pem -outform der -out "$D/Financial Analyst CA.cer"
  rm -f $D/ca.csr $D/server.pem   # force a fresh server cert under the new CA
fi

# (Re)issue the server certificate if missing, expiring within 30 days, or missing this IP/host
needs_leaf=1
if [[ -f $D/server.pem ]] && openssl x509 -checkend 2592000 -noout -in $D/server.pem >/dev/null \
   && openssl x509 -in $D/server.pem -noout -text | grep -q "DNS:$HOST" \
   && openssl x509 -in $D/server.pem -noout -text | grep -q "IP Address:$IP"; then
  needs_leaf=0
fi
if (( needs_leaf )); then
  echo "Issuing the dashboard's HTTPS certificate for $HOST ($IP)..."
  cat > $D/server.ext <<EOF
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature
extendedKeyUsage=serverAuth
subjectAltName=DNS:$HOST,DNS:localhost,IP:$IP,IP:127.0.0.1
authorityKeyIdentifier=keyid
EOF
  openssl ecparam -name prime256v1 -genkey -noout -out $D/server.key
  openssl req -new -key $D/server.key -subj "/CN=$HOST" -out $D/server.csr
  openssl x509 -req -in $D/server.csr -CA $D/ca.pem -CAkey $D/ca.key -CAcreateserial \
    -days 397 -sha256 -extfile $D/server.ext -out $D/server.pem 2>/dev/null
  rm -f $D/server.csr
fi

openssl verify -CAfile $D/ca.pem $D/server.pem >/dev/null
chmod 600 $D/*.key
