#!/usr/bin/env bash
# PR-B6: generate a self-signed CA + server certificate for the
# orchestratord peer federation listener (DESIGN_PEER_FEDERATION §6.2,
# docs/DEPLOY_PEER_FEDERATION.md §2).
#
# Usage:
#   scripts/gen_peer_tls_certs.sh [--out-dir DIR] [--hostname HOST] ...
#
# Outputs (PEM, written into --out-dir, default ./peer-tls):
#   ca.key ca.pem        — local mini-CA (3650 days); distribute ca.pem
#                          to every peer daemon that will dial this one
#                          (ORCHESTRATORD_PEER_TLS_CA)
#   server.key server.crt — TLS server cert (825 days) signed by the
#                          local CA; pass to
#                          `orchestratord serve --tls-certfile/--tls-keyfile`
#   ca.srl               — CA serial state (keep if re-issuing)
#
# The server cert carries SANs for the requested hostname(s) plus
# loopback defaults, so certificate verification works for both
# container-internal and localhost topologies.
set -euo pipefail

OUT_DIR="./peer-tls"
HOSTS=()
DAYS_CA=3650
DAYS_SERVER=825

while [ $# -gt 0 ]; do
    case "$1" in
        --out-dir) OUT_DIR="$2"; shift 2 ;;
        --hostname) HOSTS+=("$2"); shift 2 ;;
        --days-ca) DAYS_CA="$2"; shift 2 ;;
        --days-server) DAYS_SERVER="$2"; shift 2 ;;
        -h|--help)
            sed -n '2,20p' "$0"; exit 0 ;;
        *)
            echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

if [ ${#HOSTS[@]} -eq 0 ]; then
    HOSTS=("localhost")
fi

mkdir -p "$OUT_DIR"

# 1. Local mini-CA.
openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "$OUT_DIR/ca.key" -out "$OUT_DIR/ca.pem" \
    -days "$DAYS_CA" -subj "/CN=orchestratord peer CA" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null

# 2. Server key + CSR.
openssl req -newkey rsa:2048 -nodes \
    -keyout "$OUT_DIR/server.key" -out "$OUT_DIR/server.csr" \
    -subj "/CN=${HOSTS[0]}" 2>/dev/null

# 3. SAN extension file (explicit request hosts + loopback defaults).
SAN="DNS:localhost,IP:127.0.0.1,IP:::1"
for host in "${HOSTS[@]}"; do
    case "$host" in
        *.*.*.*|*:*) SAN="$SAN,IP:$host" ;;   # IPv4 / IPv6 literal
        *)           SAN="$SAN,DNS:$host" ;;
    esac
done
EXT_FILE="$(mktemp)"
trap 'rm -f "$EXT_FILE"' EXIT
cat > "$EXT_FILE" <<EOF
basicConstraints=CA:FALSE
keyUsage=digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=$SAN
EOF

# 4. Sign the server cert with the local CA.
openssl x509 -req -in "$OUT_DIR/server.csr" \
    -CA "$OUT_DIR/ca.pem" -CAkey "$OUT_DIR/ca.key" -CAcreateserial \
    -out "$OUT_DIR/server.crt" \
    -days "$DAYS_SERVER" -extfile "$EXT_FILE" 2>/dev/null
rm -f "$OUT_DIR/server.csr"

chmod 600 "$OUT_DIR/ca.key" "$OUT_DIR/server.key"

echo "generated in $OUT_DIR:"
echo "  ca.pem                     -> distribute to peer daemons (ORCHESTRATORD_PEER_TLS_CA)"
echo "  server.crt / server.key    -> orchestratord serve --tls-certfile/--tls-keyfile"
echo "verify: openssl verify -CAfile $OUT_DIR/ca.pem $OUT_DIR/server.crt"
