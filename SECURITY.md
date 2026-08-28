# Security Policy

## Supported Versions

Motet is currently pre-1.0. Security fixes are applied to the latest public release and
to the private development branch. Older public snapshots may receive fixes at the
maintainers' discretion.

## Reporting a Vulnerability

Please report suspected vulnerabilities privately by emailing `security@motet.dev`.
Include:

- Affected version or commit.
- A clear description of the issue and impact.
- Steps to reproduce, proof of concept, or relevant logs.
- Whether the issue may expose credentials, tenant data, command data, or model outputs.

Do not open a public GitHub issue for security-sensitive reports. We will acknowledge
receipt, investigate, and coordinate disclosure timing based on severity and exploitability.

## Public Repository Expectations

The public Motet repository is a clean source-available release snapshot. If you find
credentials, private infrastructure references, customer data, or internal-only material
in a public snapshot, treat it as a security issue and report it privately.

## Hardening Notes

- Keep `.env` files, credentials, TLS material, and service-account files out of git.
- Use `MOTET_REQUIRE_AUTH_FOR_OPS_ENDPOINTS=true` outside local-only development.
- Set explicit `MOTET_CORS_ALLOWED_ORIGINS` values for browser-facing deployments.
- Rotate any credential that may have been exposed in logs, traces, artifacts, or public
  repository exports.
