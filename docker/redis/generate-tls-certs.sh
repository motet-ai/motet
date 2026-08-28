#!/bin/bash
# Generate TLS certificates for Redis
# This script creates a CA and server certificates for Redis TLS

set -e

TLS_DIR="tls"
CA_KEY="${TLS_DIR}/ca.key"
CA_CRT="${TLS_DIR}/ca.crt"
REDIS_KEY="${TLS_DIR}/redis.key"
REDIS_CRT="${TLS_DIR}/redis.crt"
REDIS_CSR="${TLS_DIR}/redis.csr"
CLIENT_KEY="${TLS_DIR}/client.key"
CLIENT_CRT="${TLS_DIR}/client.crt"
CLIENT_CSR="${TLS_DIR}/client.csr"

echo "🔐 Generating Redis TLS certificates..."

# Create TLS directory
mkdir -p "${TLS_DIR}"

# Generate CA private key
echo "📝 Generating CA private key..."
openssl genrsa -out "${CA_KEY}" 4096

# Generate CA certificate
echo "📝 Generating CA certificate..."
openssl req -x509 -new -nodes -key "${CA_KEY}" -sha256 -days 365 \
  -out "${CA_CRT}" \
  -subj "/CN=Motet-Redis-CA/O=Motet/C=US"

# Generate Redis server private key
echo "📝 Generating Redis server private key..."
openssl genrsa -out "${REDIS_KEY}" 4096

# Generate Redis server certificate signing request
echo "📝 Generating Redis server certificate signing request..."
openssl req -new -key "${REDIS_KEY}" -out "${REDIS_CSR}" \
  -subj "/CN=redis-tls/O=Motet/C=US"

# Sign Redis server certificate with CA
echo "📝 Signing Redis server certificate..."
openssl x509 -req -in "${REDIS_CSR}" -CA "${CA_CRT}" -CAkey "${CA_KEY}" \
  -CAcreateserial -out "${REDIS_CRT}" -days 365 -sha256

# Generate client private key (optional, for mutual TLS)
echo "📝 Generating client private key..."
openssl genrsa -out "${CLIENT_KEY}" 4096

# Generate client certificate signing request
echo "📝 Generating client certificate signing request..."
openssl req -new -key "${CLIENT_KEY}" -out "${CLIENT_CSR}" \
  -subj "/CN=motet-client/O=Motet/C=US"

# Sign client certificate with CA
echo "📝 Signing client certificate..."
openssl x509 -req -in "${CLIENT_CSR}" -CA "${CA_CRT}" -CAkey "${CA_KEY}" \
  -CAcreateserial -out "${CLIENT_CRT}" -days 365 -sha256

# Set proper permissions
chmod 600 "${CA_KEY}" "${REDIS_KEY}" "${CLIENT_KEY}"
chmod 644 "${CA_CRT}" "${REDIS_CRT}" "${CLIENT_CRT}"

# Clean up CSR files
rm -f "${REDIS_CSR}" "${CLIENT_CSR}" "${TLS_DIR}/*.srl"

echo "✅ TLS certificates generated successfully!"
echo ""
echo "📋 Certificate files:"
echo "   CA Certificate: ${CA_CRT}"
echo "   CA Private Key: ${CA_KEY}"
echo "   Redis Server Certificate: ${REDIS_CRT}"
echo "   Redis Server Private Key: ${REDIS_KEY}"
echo "   Client Certificate: ${CLIENT_CRT}"
echo "   Client Private Key: ${CLIENT_KEY}"
echo ""
echo "⚠️  Keep these files secure! Do not commit private keys to version control."

