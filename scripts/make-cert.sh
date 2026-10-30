#!/usr/bin/env bash
# A certificate for Friday's HTTPS that phones and laptops on the LAN trust.
#
# Browsers only allow the microphone over HTTPS, and iOS only subscribes to
# calendars it trusts, but nobody can get a public certificate for a name
# like friday.local. mkcert solves this with a private root CA: install that
# one root on each phone and every certificate it signs is trusted.
#
#   sudo apt install mkcert libnss3-tools     # Ubuntu
#   brew install mkcert                        # macOS
#   scripts/make-cert.sh                       # writes data/certs/{cert,key}.pem
#
# Then install the root on each device (once):
#   Android / iPhone: send yourself the file printed below, open it, and on
#   iOS also enable it under Settings > General > About > Certificate Trust
#   Settings. Mac: double-click it into Keychain and set Always Trust.
#
# Without mkcert this falls back to a plain self-signed certificate, which
# works but every device warns about it separately.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p data/certs
HOSTS=(friday.local localhost 127.0.0.1)
# Every LAN address the machine currently has, so the IP works too.
if [[ "$(uname -s)" == Darwin ]]; then
  for ifc in en0 en1; do
    ip=$(ipconfig getifaddr "$ifc" 2>/dev/null || true)
    if [[ -n "$ip" ]]; then HOSTS+=("$ip"); fi
  done
else
  for ip in $(hostname -I 2>/dev/null); do HOSTS+=("$ip"); done
fi
if command -v mkcert >/dev/null 2>&1; then
  mkcert -install >/dev/null
  mkcert -cert-file data/certs/cert.pem -key-file data/certs/key.pem "${HOSTS[@]}"
  echo
  echo "Root CA to install on phones and laptops: $(mkcert -CAROOT)/rootCA.pem"
else
  # No mkcert: do the same thing with openssl. A private root CA signs a
  # short-lived certificate for the house. The root is what goes on each
  # phone, once — the certificate underneath it can then be reissued (a new
  # router, a new address, next year) without touching a single device.
  #
  # The certificate itself is deliberately short: Apple refuses TLS
  # certificates valid for more than 398 days, and a self-signed one that
  # ran to 825 days is what put "This Connection Is Not Private" on a phone
  # that had been working all day.
  echo "mkcert not found; using openssl with a private root CA instead." >&2
  SAN=""
  for host in "${HOSTS[@]}"; do
    if [[ "$host" =~ ^[0-9.]+$ ]]; then SAN+="IP:$host,"; else SAN+="DNS:$host,"; fi
  done
  SAN="${SAN%,}"
  if [[ ! -f data/certs/rootCA.pem || ! -f data/certs/rootCA-key.pem ]]; then
    echo "Making the house's root certificate authority (once)." >&2
    openssl req -x509 -quiet -newkey rsa:4096 -sha256 -days 3650 -nodes \
      -keyout data/certs/rootCA-key.pem -out data/certs/rootCA.pem \
      -subj "/CN=House Agent Root CA/O=House Agent" \
      -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
      -addext "keyUsage=critical,keyCertSign,cRLSign"
    chmod 600 data/certs/rootCA-key.pem
  fi
  openssl req -new -quiet -newkey rsa:2048 -nodes \
    -keyout data/certs/key.pem -out data/certs/house.csr \
    -subj "/CN=friday.local/O=House Agent"
  openssl x509 -req -in data/certs/house.csr -sha256 -days 397 \
    -CA data/certs/rootCA.pem -CAkey data/certs/rootCA-key.pem -CAcreateserial \
    -out data/certs/cert.pem \
    -extfile <(printf 'subjectAltName=%s\nbasicConstraints=CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n' "$SAN") 2>/dev/null
  rm -f data/certs/house.csr
  chmod 600 data/certs/key.pem
  echo
  echo "Root CA to install on phones and laptops: data/certs/rootCA.pem"
  echo "Install it once per device; this certificate expires $(openssl x509 -in data/certs/cert.pem -noout -enddate | cut -d= -f2)."
fi
echo "Restart Friday to use the new certificate."
