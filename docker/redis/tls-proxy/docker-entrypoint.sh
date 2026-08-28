#!/bin/sh
# Motet redis-tls proxy entrypoint.
# stunnel needs a server certificate. A clean clone (and motet-eval) has no
# tls/ material because those files are gitignored, so generate ephemeral
# certs when the mounted volume is empty.

set -e

CERT_DIR="/tls"
CONF="/etc/stunnel/stunnel.conf"

if [ ! -f "${CERT_DIR}/redis.crt" ] || [ ! -f "${CERT_DIR}/redis.key" ]; then
  CERT_DIR="/tmp/motet-tls"
  mkdir -p "${CERT_DIR}"
  echo "tls/ has no Redis server certificate; generating ephemeral material for local evaluation"
  openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "${CERT_DIR}/redis.key" \
    -out "${CERT_DIR}/redis.crt" \
    -days 365 \
    -subj "/CN=redis-tls/O=Motet/C=US"
  cp "${CERT_DIR}/redis.crt" "${CERT_DIR}/ca.crt"
  CONF="${CERT_DIR}/stunnel.conf"
  cat > "${CONF}" <<EOF
foreground = yes
pid =

[redis-tls]
client = no
accept = 0.0.0.0:6380
connect = redis:6379
cert = ${CERT_DIR}/redis.crt
key = ${CERT_DIR}/redis.key
CAfile = ${CERT_DIR}/ca.crt
verify = 0
options = NO_SSLv2
options = NO_SSLv3
TIMEOUTidle = 86400
TIMEOUTclose = 0
EOF
fi

exec stunnel "${CONF}"
