# Redis TLS

Local Compose talks to Valkey through the `redis-tls` stunnel proxy
(`rediss://redis-tls:6380`). The proxy loads `tls/redis.crt` and `tls/redis.key`.

`tls/` is gitignored. A clean clone does not include those files.

## What creates the certificates

- `motet-cli local up` and `motet-cli local recreate` write `tls/` when it is
  missing. They prefer `docker/redis/generate-tls-certs.sh`, and fall back to
  host `openssl`.
- The `redis-tls` image entrypoint generates ephemeral certificates inside the
  container when the mounted `tls/` volume has no server pair. Application
  services use `ssl_cert_reqs=none`, so they do not need the host `tls/` CA.

## Generate certificates yourself

```bash
./docker/redis/generate-tls-certs.sh
```

This script creates:

- `tls/ca.crt` / `tls/ca.key` — Certificate Authority
- `tls/redis.crt` / `tls/redis.key` — Redis server certificate
- `tls/client.crt` / `tls/client.key` — client certificate (optional mutual TLS)

Validity is 365 days. Private keys are mode `600`.

## Sign-in failures

SSO stores OAuth state in Redis through `redis-tls`. If the proxy is
crash-looping, Sign in with SSO returns `Name or service not known` for
`redis-tls:6380`. Recreate the stack after `tls/` exists:

```bash
motet-cli local down
motet-cli local up --build
```
