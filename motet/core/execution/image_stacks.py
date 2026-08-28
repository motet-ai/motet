"""
Motet - Platform image-stack registry

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

What is an image stack?
-----------------------
"Image stack" is Motet's term for the **curated base image layer** that a
bundle's exec image is built on top of. It is analogous to a Cloud Native
Buildpacks `stack`_ (a maintained build/run base-image pair) and is
**orthogonal** to 's *isolation tiers* (``runc`` / ``runsc`` / Kata-fc),
which describe **how** a container runs, not **what is inside it**.

A bundle pins which stack it targets via ``config/exec.yaml``::

    base_image_stack: python-minimal
    requirements_path: exec/requirements.txt

The deployer combines the resolved
stack image with the bundle's ``requirements.txt`` to produce a
``image@sha256:...`` ref it pins into the catalog.

.. _stack: https://buildpacks.io/docs/concepts/components/stack/

Why a registry?
---------------
Three downstream consumers need to ask the same question — *"is X a known
stack and what image does it resolve to?"*:

1. **Validate / publish lint** — warn operators when
   ``base_image_stack`` is set to something the platform does not know.
2. **Deployer build orchestration** —
   resolve the stack name to an OCI ref and pass it as ``BASE_IMAGE`` to the
   in-repo Dockerfile so the bake-on-top pattern uses the right base.
3. **Ops UI** (BundlesPage / future stacks page) — show operators which stacks
   exist, which are pinned, and which still need to be wired up.

A single registry source-of-truth keeps all three honest.

Configuration model
-------------------
The registry has two tiers (in this order; later wins):

1. **Builtins** — a small list of names every Motet install knows about
because they are referenced by the in-repo Dockerfile /. Builtins
   that are not actually pinned to a usable image have ``oci_image_ref=""``
   (an honest "this is a placeholder until an operator fills it in").

2. **Env overrides / additions** — operators populate or add stacks via env::

       MOTET_IMAGE_STACK_PYTHON_MINIMAL=registry.example.com/motet/python-minimal@sha256:...
       MOTET_IMAGE_STACK_PYTHON_MINIMAL_DESCRIPTION=Python 3.11 + stdlib + small wheels
    MOTET_IMAGE_STACK_PYTHON_DATA_SCIENCE=registry.example.com/motet/python-ds@sha256:...
    MOTET_IMAGE_STACK_PYTHON_DATA_SCIENCE_CAPABILITIES=python,numpy,pandas

   The mapping is ``MOTET_IMAGE_STACK_<NAME>`` → ``oci_image_ref``. ``<NAME>``
   is uppercased / underscored when set in env and lowercased / dashed for the
   stack name (so ``MOTET_IMAGE_STACK_PYTHON_MINIMAL`` registers
   ``python-minimal``). An optional ``..._DESCRIPTION`` companion var sets the
   human-readable description.

This config-by-env shape is deliberate — it matches the rest of Motet's
runtime configuration surface (no new YAML loader, no service to deploy) and
keeps the v1 of the registry trivial. A YAML-file mode is a follow-up if the
env surface becomes unwieldy.

Out of scope for this slice
---------------------------
- Per-stack network policy, per-stack capability filters, GPU / arch hints.
Those are concerns and will land alongside the sandbox manager.
- A dedicated ``/api/v1/exec/image-stacks`` *write* endpoint. Stacks are
  platform configuration, not tenant configuration; operators set them via
  env (or a future config file), not via API call.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Public data shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImageStack:
    """A platform-managed base image stack.

    Attributes:
        name: Stack name as referenced by ``config/exec.yaml``
            ``base_image_stack`` (e.g. ``"python-minimal"``). Lowercase,
            dash-separated by convention.
        oci_image_ref: Resolved OCI ref (recommended ``image@sha256:...``).
            Empty string when the stack is registered as a placeholder
            (e.g. a builtin that has not been wired to a concrete image yet).
        description: Human-readable summary for the ops UI / docs.
        builtin: True if the stack is in the in-repo builtin list (operators
            may still override its ``oci_image_ref`` / ``description`` via env).
    """

    name: str
    oci_image_ref: str = ""
    description: str = ""
    builtin: bool = False
    capabilities: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_pinned(self) -> bool:
        """True when ``oci_image_ref`` is set (any non-empty string).

        Note: this does **not** validate the ref is digest-pinned —
        ADR-0100 §rule 2 (``MOTET_REQUIRE_DIGEST_PINNED_PUBLISH``) is the
        gate for that. ``is_pinned`` only answers "does this stack resolve
        to a usable image at all".
        """
        return bool(self.oci_image_ref.strip())

    def supports_all(self, required: Tuple[str, ...]) -> bool:
        """Return whether this stack declares every required runtime capability."""
        have = set(self.capabilities)
        return all(item in have for item in required)


@dataclass(frozen=True)
class ImageStackResolution:
    """Result of resolving runtime capabilities to an image stack."""

    required_capabilities: Tuple[str, ...]
    stack: Optional[ImageStack] = None
    missing_capabilities: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def matched(self) -> bool:
        return self.stack is not None and not self.missing_capabilities


# ---------------------------------------------------------------------------
# Builtins
# ---------------------------------------------------------------------------
#
# These are the names Motet ships knowing about. ``python-minimal`` is the
# only one that maps to a usable image out of the box (it matches the FROM
# line in ``docker/images/bundle-exec-from-requirements/Dockerfile``). The
# other two are reserved by ADR-0101 — registered as placeholders so the
# UI can show "exists but unpinned" and the lint can accept them as known
# names — but operators MUST point ``MOTET_IMAGE_STACK_PYTHON_OFFICE`` /
# ``..._BROWSER`` at real images before bundles can build against them.
#
# Adding a builtin here is a deliberate platform choice; adding an arbitrary
# new stack name is what env vars exist for.

_BUILTIN_STACKS: List[ImageStack] = [
    ImageStack(
        name="python-minimal",
        oci_image_ref="python:3.11-slim",
        description="Python 3.11 + stdlib + small wheels (requests, pydantic).",
        builtin=True,
        capabilities=("python",),
    ),
    ImageStack(
        name="python-office",
        oci_image_ref="",
        description=(
            "Python 3.11 + python-docx, openpyxl, python-pptx, pypdf, LibreOffice. "
            "Operator must pin via MOTET_IMAGE_STACK_PYTHON_OFFICE before use."
        ),
        builtin=True,
        capabilities=(
            "python",
            "office",
            "docx",
            "pptx",
            "xlsx",
            "pdf",
            "libreoffice",
            "soffice",
            "poppler",
            "ghostscript",
        ),
    ),
    ImageStack(
        name="python-browser",
        oci_image_ref="",
        description=(
            "Python 3.11 + Playwright + Chromium (headless). "
            "Operator must pin via MOTET_IMAGE_STACK_PYTHON_BROWSER before use."
        ),
        builtin=True,
        capabilities=("python", "browser", "chromium", "playwright"),
    ),
]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


_ENV_PREFIX = "MOTET_IMAGE_STACK_"
_ENV_DESC_SUFFIX = "_DESCRIPTION"
_ENV_CAPABILITIES_SUFFIX = "_CAPABILITIES"
# Stack names are lowercase + dash-separated. We restrict to a conservative
# character set so they never need shell-escaping, never collide with
# Dockerfile-arg syntax, and round-trip cleanly through env var names.
_STACK_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$|^[a-z0-9]$")


def _stack_name_from_env(env_key: str) -> Optional[str]:
    """Convert ``MOTET_IMAGE_STACK_PYTHON_MINIMAL`` → ``"python-minimal"``.

    Returns ``None`` when the env key does not match the prefix or yields a
    name that would not pass :data:`_STACK_NAME_RE` (e.g. starts with a digit-
    looking pattern but contains underscores we'd refuse to translate).
    """
    if not env_key.startswith(_ENV_PREFIX):
        return None
    if env_key.endswith(_ENV_DESC_SUFFIX) or env_key.endswith(_ENV_CAPABILITIES_SUFFIX):
        return None
    tail = env_key[len(_ENV_PREFIX):]
    if not tail:
        return None
    name = tail.lower().replace("_", "-")
    if not _STACK_NAME_RE.match(name):
        return None
    return name


@dataclass
class _RegistryView:
    """Internal cache of the resolved (builtin + env) stack table."""

    by_name: Dict[str, ImageStack] = field(default_factory=dict)


def _build_registry(env: Optional[Dict[str, str]] = None) -> _RegistryView:
    """Compose the live registry from builtins + env overrides.

    ``env`` is injectable for testing; production callers pass ``None`` to
    use ``os.environ`` at call time. The registry is **not** cached at module
    scope on purpose — env vars can change across worker boots and unit tests
    routinely use ``monkeypatch.setenv`` to assert behavior.
    """
    env_map: Dict[str, str] = dict(env if env is not None else os.environ)

    by_name: Dict[str, ImageStack] = {s.name: s for s in _BUILTIN_STACKS}

    # Pass 1: env-set refs override builtins or register new stacks.
    for key, value in env_map.items():
        if key.endswith(_ENV_DESC_SUFFIX) or key.endswith(_ENV_CAPABILITIES_SUFFIX):
            continue
        name = _stack_name_from_env(key)
        if name is None:
            continue
        ref = (value or "").strip()
        existing = by_name.get(name)
        if existing is not None:
            by_name[name] = ImageStack(
                name=name,
                oci_image_ref=ref,
                description=existing.description,
                builtin=existing.builtin,
                capabilities=existing.capabilities,
            )
        else:
            by_name[name] = ImageStack(
                name=name,
                oci_image_ref=ref,
                description="",
                builtin=False,
                capabilities=(),
            )

    # Pass 2: env-set descriptions override (must come second so we know the
    # stack name is registered).
    for key, value in env_map.items():
        if not key.endswith(_ENV_DESC_SUFFIX):
            continue
        base_key = key[: -len(_ENV_DESC_SUFFIX)]
        name = _stack_name_from_env(base_key)
        if name is None or name not in by_name:
            continue
        existing = by_name[name]
        by_name[name] = ImageStack(
            name=name,
            oci_image_ref=existing.oci_image_ref,
            description=value,
            builtin=existing.builtin,
            capabilities=existing.capabilities,
        )

    # Pass 3: env-set capabilities override builtins or custom stacks.
    for key, value in env_map.items():
        if not key.endswith(_ENV_CAPABILITIES_SUFFIX):
            continue
        base_key = key[: -len(_ENV_CAPABILITIES_SUFFIX)]
        name = _stack_name_from_env(base_key)
        if name is None or name not in by_name:
            continue
        existing = by_name[name]
        by_name[name] = ImageStack(
            name=name,
            oci_image_ref=existing.oci_image_ref,
            description=existing.description,
            builtin=existing.builtin,
            capabilities=_normalize_capabilities(value.split(",")),
        )

    return _RegistryView(by_name=by_name)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def list_image_stacks(env: Optional[Dict[str, str]] = None) -> List[ImageStack]:
    """Return all registered stacks, sorted by name.

    Builtins always appear (even when unpinned). Env-added stacks appear
    interleaved alphabetically. Ordering is stable so the API/UI doesn't
    flicker between calls.
    """
    view = _build_registry(env)
    return sorted(view.by_name.values(), key=lambda s: s.name)


def resolve_image_stack(
    name: str,
    env: Optional[Dict[str, str]] = None,
) -> Optional[ImageStack]:
    """Look up a stack by name.

    Returns ``None`` when the name is not registered — distinct from
    "registered but unpinned" (where the returned :class:`ImageStack`
    has ``oci_image_ref == ""``). Callers that want to gate on "pinned"
    should check :attr:`ImageStack.is_pinned`.
    """
    if not name:
        return None
    view = _build_registry(env)
    return view.by_name.get(name.strip())


def is_known_stack(name: str, env: Optional[Dict[str, str]] = None) -> bool:
    """Convenience for lints: is this name in the registry at all?"""
    return resolve_image_stack(name, env=env) is not None


def resolve_image_stack_for_capabilities(
    required: List[str] | Tuple[str, ...],
    env: Optional[Dict[str, str]] = None,
    *,
    require_pinned: bool = True,
) -> ImageStackResolution:
    """Resolve runtime capabilities to the smallest matching stack.

    Matching is intentionally deterministic: prefer stacks with fewer
    capabilities, then builtins, then lexicographic stack name. By default only
    pinned stacks are considered usable because callers need a concrete image
    for workspace execution.
    """
    normalized = _normalize_capabilities(required)
    if not normalized:
        return ImageStackResolution(required_capabilities=())

    candidates = [
        stack
        for stack in list_image_stacks(env=env)
        if (stack.is_pinned or not require_pinned) and stack.supports_all(normalized)
    ]
    if candidates:
        candidates.sort(key=lambda s: (len(s.capabilities), not s.builtin, s.name))
        return ImageStackResolution(required_capabilities=normalized, stack=candidates[0])

    known = set()
    for stack in list_image_stacks(env=env):
        if stack.is_pinned or not require_pinned:
            known.update(stack.capabilities)
    missing = tuple(item for item in normalized if item not in known)
    if not missing:
        # Capabilities exist individually but not in one usable stack.
        missing = normalized
    return ImageStackResolution(required_capabilities=normalized, missing_capabilities=missing)


_CAPABILITY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$|^[a-z0-9]$")


def _normalize_capabilities(values: List[str] | Tuple[str, ...]) -> Tuple[str, ...]:
    out: List[str] = []
    seen: set[str] = set()
    for raw in values:
        value = str(raw or "").strip().lower().replace("_", "-")
        if not value or not _CAPABILITY_RE.match(value) or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return tuple(out)
