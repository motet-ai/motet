# Versioning / FSL

A Motet **product version** is a published snapshot of the runtime (and, while lockstep, the SDK) made available outside the private canonical repository. Private commits, PRs, and file edits are not versions.

Typical availability: tagged export to the evaluation/public repo, invite-only access to that snapshot, or publishing a package or convenience image of it.

One convenience image set per product version (`vX.Y.Z`). Invite-only snapshots keep those packages private. A public Motet release uses the same tags publicly. Do not invent a second image line.

Do not live-mirror `main` into a public FSL repository. Do not publish a new public image from every `main` commit.

## SemVer, on `0.y.z` until 1.0

| Bump | When |
|---|---|
| Patch (`0.1.1`) | Fixes on a snapshot already given to someone |
| Minor (`0.2.0`) | A new snapshot prospects should take |
| Major (`1.0.0`+) | Breaking public SDK, command, or HTTP contract — and only after we intend to support it |

`0.x` means no compatibility guarantee. Do not use calendar versioning as the product version.

Runtime and SDK share `X.Y.Z` until a compatibility matrix exists.

## One version string

Canonical source is PEP 621 `[project].version`:

- Root `pyproject.toml` for `motet`
- `motet-sdk/pyproject.toml` for `motet-sdk`

Runtime and HTTP read `importlib.metadata.version("motet")`. Do not invent a second literal. Do not put the product version in file headers, ADRs, or comments.

Between releases, keep the last released version or use a development marker such as `0.2.0.dev0`. Do not bump `X.Y.Z` on every merge.

These are **not** the product version: `/api/v1`, image SHAs, `latest` (alias), bundle tree hashes, `AGENTS.md` / ADR document versions.

## FSL clock

For each published **runtime** snapshot:

- **Release date** = first availability (export, invite access, package, or convenience image — earliest)
- **FSL conversion date** = release date + 2 years
- Record both in `PUBLIC_RELEASE.md` on the exported tree and date the matching `CHANGELOG.md` section
- Tag the **exported** repository `vX.Y.Z`

The SDK is Apache 2.0 from first publication. It has no FSL conversion clock.

HEAD on the private repository has no conversion date.

## Three date systems

| Date | What it is | Starts the FSL clock? |
|---|---|---|
| Copyright year range | Redistribution notice | No |
| File header `Last Modified` | Engineering freshness | No |
| Release date in `PUBLIC_RELEASE.md` / `CHANGELOG` / tag | When that version was made available | **Yes** |

Do not put a `Created:` date in Python source headers. Document-level `Date Created` on ADRs and `AGENTS.md` versions the document, not a product snapshot.

## How this tree versions

`docs/architecture/current/` describes HEAD toward the next snapshot. A published `vX.Y.Z` export includes this tree and **nothing else** from `docs/architecture/` (no decisions, design notes, audits, or allowlists).

Release date and FSL conversion date for a published snapshot are recorded in `PUBLIC_RELEASE.md` on that tree.
