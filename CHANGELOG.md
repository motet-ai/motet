# Changelog

All notable changes to the Motet project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.2] - 2026-08-31

### Added
- Isolated spawn-child conversations: each `core.spawn_agents` child is its own
  conversation with parent/root pointers, a first-turn brief and reply, and a card
  on the parent turn. Chat Explorer opens those cards as real chats.
- `core.subagent` follow-up while the child stays listed under the parent chat agent.
- `parent_conversation_id` on conversation list and GET (`null` for roots).
- Persist and restore thinking, tool summaries, and chat cost across reload.
- Chat Explorer can stream more than one conversation at once and shows in-flight
  list state.
- Evaluation/public snapshots include the Chat Explorer and Manage screenshots
  (`docs/images/`) so the README and Chat Explorer onboarding embeds render.

### Changed
- Deleting a parent conversation also clears its isolated children.
- `prepare_context` skips empty-query memory recall (the 60s timeout path).
- Expert-panel `discuss` puts the topic on a user message and defaults to o3-mini.

### Fixed
- Memory store no longer files a row under a caller-supplied `conversation_id`.
- A spawn card is kept for each child even when one transcript persist fails.
- Chat Explorer pins the live stream and auto-title to the owning conversation.

## [0.1.1] - 2026-08-28

### Added
- Local-stack sign-in docs: seeded Keycloak users (`motet-admin`, `acme-user`) in Quick Start
  and Chat Explorer, so the first `local up` has a path through the SSO page.

### Changed
- Prometheus series on `/metrics` use the `motet_` prefix (`motet_requests_total`,
  `motet_auth_attempts_total`, tool/model/breaker/scheduler series). The `imf_`
  names are not emitted.
- Public quick start no longer requires `docker login ghcr.io` for `ghcr.io/motet-ai` images.

## [0.1.0] - 2026-06-18

### Added
- Public/prospect repository policy describing the clean-history source-available
  distribution model.
- Public export script for generating FSL/Apache-preserving release snapshots.
- `.env.example`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, and `COMMERCIAL_LICENSE.md`
  for public repository readiness.
- Pre-release code quality review and remediation (ADR-0091)
- `CONTRIBUTING.md` for external contributors
- This `CHANGELOG.md` for tracking project changes
- Configurable CORS origins via `MOTET_CORS_ALLOWED_ORIGINS` (replaces wildcard default)
- Configurable vault salt via `MOTET_VAULT_SALT` (replaces hardcoded salt)
- Optional auth gating for operational endpoints via `MOTET_REQUIRE_AUTH_FOR_OPS_ENDPOINTS`

### Changed
- ADR-0080 amended to make the runtime source-available under FSL/commercial terms via
  a clean public/prospect repository, with hardened images reserved for managed or
  enterprise packaging.
- README and onboarding docs updated for the `motet-ai/motet` public repository.
- Debug endpoints (`/api/v1/debug/*`) now require authentication in addition to debug mode
- Auth debug claims endpoint (`/api/v1/auth/debug/claims`) now requires debug mode and authentication
- Auth debug claims endpoint no longer returns raw JWT claims or token previews on error
- CORS middleware only added when origins are explicitly configured (no more wildcard default)

### Security
- Fixed CORS configuration allowing credentials from any origin
- Fixed unauthenticated access to debug endpoints that could expose command data and allow task deletion
- Fixed unauthenticated JWT claims inspection endpoint that decoded tokens without verification
- Fixed hardcoded PBKDF2 salt in vault service
- Added optional authentication for `/metrics`, `/ops`, and `/managers/status` endpoints

### Fixed
- License references in README.md now correctly reference FSL-1.1-ALv2 and Apache 2.0 (was incorrectly stating MIT)
- Eliminated all bare `except:` clauses (10 instances across 5 files) — replaced with `except Exception:`
- Added justifying comments and debug logging to ~85 silent `except Exception: pass` blocks across the codebase
- Created GitHub issue (#80) to track resolution of 20+ TODO/FIXME/HACK comments
