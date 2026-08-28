"""
Motet — Skill runners.yaml parser

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Parses the per-skill ``runners.yaml`` manifest defined by.
    A ``runners.yaml`` declares one or more *runners* — named entrypoints
    that the platform turns into tools the LLM can call. Each runner ships
    its own execution policy (image stack, network, credentials, timeout,
    optional lifetime) so authors do not have to know about the underlying
    tool surface.

    File location:

        skills/<skill-dir>/runners.yaml

    The schema this module pins corresponds to Slice B. ``lifetime``
    is the author-facing execution-lifetime field:
    ``ephemeral`` (fresh execution), ``workspace`` (persistent /scratch,
    fresh process), or ``stateful`` (persistent process).

Dependencies:
    - PyYAML (already a runtime dep)

Usage:
    from motet.core.skills.runners import parse_runners_yaml

    doc = parse_runners_yaml(path)
    for runner in doc.runners:
        ...

Notes:
    - The parser is intentionally strict about *types* and *required keys*
      so deployment fails loudly rather than registering a malformed
      runner. §"runners.yaml" is the source of truth for the
      schema; this module is the authoritative implementation.
    - Runner names must be lowercase ``[a-z0-9-_]+`` so they compose into
      tool names without quoting (``{bundle_id}.{skill_name}.{runner}``).
    - ``script`` is a *bundle-relative* path under the skill directory.
      The runtime registration step composes the staged-bundle path
      (``skills/<skill>/<script>``) before dispatching.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple


RunnerLifetime = Literal["ephemeral", "workspace", "stateful"]
SUPPORTED_LIFETIME_VALUES = ("ephemeral", "workspace", "stateful")
SUPPORTED_INTERPRETERS = ("python", "python3", "bash", "sh", "node")
SUPPORTED_ARG_TYPES = ("string", "integer", "number", "boolean")
SUPPORTED_NETWORK_MODES = ("none", "restricted", "inherit")

_RUNNER_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_RUNNER_NAME_MAX_LEN = 64
_RUNNER_DESCRIPTION_MAX_LEN = 1024
_RUNNER_TIMEOUT_HARD_CEILING_SECONDS = 3600


@dataclass(frozen=True)
class RunnerArg:
    """A single named argument declared by a runner.

    The runtime turns each declared arg into a CLI flag of the form
    ``--{name}=value`` when invoking the script. Authors who need
    positional arguments or different flag styles must use ``core.worker_exec``
    directly; runners optimize for the common case.
    """

    name: str
    type: str
    description: str = ""
    default: Any = None
    required: bool = False


@dataclass(frozen=True)
class RunnerSpec:
    """A single runner declaration.

    Represents one named entrypoint that becomes a tool at deploy time.
    The fields mirror ADR-0101 §"runners.yaml" with explicit defaults so
    the runtime never has to invent policy.
    """

    name: str
    description: str
    script: str
    interpreter: str = "python3"
    image_stack: str = "python-minimal"
    lifetime: RunnerLifetime = "ephemeral"
    timeout_seconds: Optional[int] = None
    network: str = "inherit"
    credentials: Tuple[str, ...] = field(default_factory=tuple)
    args: Tuple[RunnerArg, ...] = field(default_factory=tuple)
    raw: Dict[str, Any] = field(default_factory=dict)

    def tool_name(self, bundle_id: str, skill_name: str) -> str:
        """Compose the namespaced tool name this runner registers as."""
        return f"{bundle_id}.{skill_name}.{self.name}"


@dataclass(frozen=True)
class RunnersDoc:
    """Parsed contents of a ``skills/<dir>/runners.yaml``."""

    runners: Tuple[RunnerSpec, ...]
    raw: Dict[str, Any]


# ---------------------------------------------------------------------------
# Public parsers
# ---------------------------------------------------------------------------


def parse_runners_yaml(path: Path) -> RunnersDoc:
    """Read and validate a ``runners.yaml`` from disk.

    Raises:
        ValueError: missing file, invalid YAML, or any schema violation.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    return parse_runners_yaml_text(text, source_hint=str(path))


def parse_runners_yaml_text(text: str, source_hint: str = "") -> RunnersDoc:
    """Parse runners.yaml content from a string.

    The string-input form is the test seam and is what the bundle linter
    uses against the in-memory bundle file dict.
    """
    source = source_hint or "runners.yaml"
    try:
        import yaml  # type: ignore[import]

        loaded = yaml.safe_load(text)
    except Exception as exc:
        raise ValueError(f"Invalid YAML in {source}: {exc}") from exc

    if loaded is None:
        # Empty file is treated as "no runners declared"; valid but useless.
        return RunnersDoc(runners=(), raw={})

    if not isinstance(loaded, dict):
        raise ValueError(f"runners.yaml top-level must be a mapping in {source}")

    runners_raw = loaded.get("runners")
    if runners_raw is None:
        # Authors may omit the key entirely if they intend an empty manifest.
        return RunnersDoc(runners=(), raw=loaded)
    if not isinstance(runners_raw, list):
        raise ValueError(
            f"runners.yaml field 'runners' must be a list in {source}"
        )

    parsed: List[RunnerSpec] = []
    seen_names: set[str] = set()
    for index, raw_entry in enumerate(runners_raw):
        if not isinstance(raw_entry, dict):
            raise ValueError(
                f"runners[{index}] must be a mapping in {source}"
            )
        spec = _parse_one_runner(raw_entry, index=index, source=source)
        if spec.name in seen_names:
            raise ValueError(
                f"duplicate runner name {spec.name!r} in {source} "
                "(runner names must be unique within a skill)"
            )
        seen_names.add(spec.name)
        parsed.append(spec)

    return RunnersDoc(runners=tuple(parsed), raw=loaded)


# ---------------------------------------------------------------------------
# Per-runner parsing
# ---------------------------------------------------------------------------


def _parse_one_runner(raw: Dict[str, Any], *, index: int, source: str) -> RunnerSpec:
    where = f"runners[{index}] in {source}"

    name = _required_str(raw, "name", where=where)
    if len(name) > _RUNNER_NAME_MAX_LEN:
        raise ValueError(
            f"runner 'name' exceeds {_RUNNER_NAME_MAX_LEN} characters in {where}"
        )
    if not _RUNNER_NAME_RE.match(name):
        raise ValueError(
            "runner 'name' must use lowercase letters, digits, '-' or '_' (and start "
            f"with a letter or digit) in {where}"
        )

    description = _required_str(raw, "description", where=where)
    if len(description) > _RUNNER_DESCRIPTION_MAX_LEN:
        raise ValueError(
            f"runner 'description' exceeds {_RUNNER_DESCRIPTION_MAX_LEN} characters in {where}"
        )

    script = _required_str(raw, "script", where=where)
    if script.startswith("/") or ".." in Path(script).parts:
        raise ValueError(
            f"runner 'script' must be a bundle-relative path without '..' in {where}"
        )

    interpreter = _optional_str(raw, "interpreter", default="python3", where=where)
    if interpreter not in SUPPORTED_INTERPRETERS:
        raise ValueError(
            f"runner 'interpreter' must be one of {SUPPORTED_INTERPRETERS} in {where}"
        )

    image_stack = _optional_str(
        raw, "image_stack", default="python-minimal", where=where
    )
    if not image_stack:
        raise ValueError(f"runner 'image_stack' must be non-empty in {where}")

    lifetime = _parse_lifetime(raw, where=where)

    timeout_seconds = _parse_timeout(raw.get("timeout_seconds"), where=where)

    network = _optional_str(raw, "network", default="inherit", where=where)
    if network not in SUPPORTED_NETWORK_MODES:
        raise ValueError(
            f"runner 'network' must be one of {SUPPORTED_NETWORK_MODES} in {where}"
        )

    credentials = _parse_credentials(raw.get("credentials"), where=where)
    args = _parse_args(raw.get("args"), where=where)

    return RunnerSpec(
        name=name,
        description=description.strip(),
        script=script,
        interpreter=interpreter,
        image_stack=image_stack,
        lifetime=lifetime,
        timeout_seconds=timeout_seconds,
        network=network,
        credentials=credentials,
        args=args,
        raw=dict(raw),
    )


def _parse_lifetime(raw_entry: Dict[str, Any], *, where: str) -> RunnerLifetime:
    if "session" in raw_entry:
        raise ValueError(
            "runner field 'session' is no longer supported; use "
            "'lifetime: ephemeral|workspace|stateful' in "
            f"{where}"
        )

    raw = raw_entry.get("lifetime")
    if raw is None:
        return "ephemeral"
    if isinstance(raw, bool):
        # YAML's `lifetime: false` parses as bool; tolerate false as the
        # default but reject true because it is ambiguous.
        if raw is False:
            return "ephemeral"
        raise ValueError(
            f"runner 'lifetime' must be one of {SUPPORTED_LIFETIME_VALUES} in {where}"
        )
    if not isinstance(raw, str):
        raise ValueError(f"runner 'lifetime' must be a string in {where}")
    token = raw.strip().lower()
    if token in SUPPORTED_LIFETIME_VALUES:
        return token  # type: ignore[return-value]
    raise ValueError(
        f"runner 'lifetime' must be one of {SUPPORTED_LIFETIME_VALUES} in {where}"
    )


def _parse_timeout(raw: Any, *, where: str) -> Optional[int]:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(f"runner 'timeout_seconds' must be a positive integer in {where}")
    if raw <= 0:
        raise ValueError(f"runner 'timeout_seconds' must be > 0 in {where}")
    if raw > _RUNNER_TIMEOUT_HARD_CEILING_SECONDS:
        raise ValueError(
            f"runner 'timeout_seconds' must be <= "
            f"{_RUNNER_TIMEOUT_HARD_CEILING_SECONDS} in {where}"
        )
    return int(raw)


def _parse_credentials(raw: Any, *, where: str) -> Tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"runner 'credentials' must be a list of strings in {where}")
    out: List[str] = []
    seen: set[str] = set()
    for i, value in enumerate(raw):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f"runner 'credentials[{i}]' must be a non-empty string in {where}"
            )
        token = value.strip()
        if token in seen:
            raise ValueError(
                f"runner 'credentials' contains duplicate entry {token!r} in {where}"
            )
        seen.add(token)
        out.append(token)
    return tuple(out)


def _parse_args(raw: Any, *, where: str) -> Tuple[RunnerArg, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise ValueError(
            f"runner 'args' must be a mapping of name -> spec in {where}"
        )
    parsed: List[RunnerArg] = []
    for name, spec in raw.items():
        if not isinstance(name, str) or not _RUNNER_NAME_RE.match(name):
            raise ValueError(
                f"runner arg name {name!r} must be lowercase [a-z0-9_-]+ in {where}"
            )
        if not isinstance(spec, dict):
            raise ValueError(
                f"runner arg {name!r} must be a mapping in {where}"
            )
        a_type = spec.get("type")
        if not isinstance(a_type, str) or a_type not in SUPPORTED_ARG_TYPES:
            raise ValueError(
                f"runner arg {name!r} 'type' must be one of {SUPPORTED_ARG_TYPES} in {where}"
            )
        description = spec.get("description") or ""
        if not isinstance(description, str):
            raise ValueError(
                f"runner arg {name!r} 'description' must be a string in {where}"
            )
        required = bool(spec.get("required", False))
        default = spec.get("default")
        if default is not None and not _arg_value_matches_type(default, a_type):
            raise ValueError(
                f"runner arg {name!r} default does not match declared type {a_type!r} in {where}"
            )
        parsed.append(
            RunnerArg(
                name=name,
                type=a_type,
                description=description.strip(),
                default=default,
                required=required,
            )
        )
    return tuple(parsed)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _required_str(loaded: Dict[str, Any], key: str, *, where: str) -> str:
    if key not in loaded:
        raise ValueError(f"runner missing required field {key!r} in {where}")
    value = loaded.get(key)
    if not isinstance(value, str):
        raise ValueError(f"runner field {key!r} must be a string in {where}")
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"runner field {key!r} must be non-empty in {where}")
    return stripped


def _optional_str(
    loaded: Dict[str, Any], key: str, *, default: str, where: str
) -> str:
    if key not in loaded or loaded.get(key) is None:
        return default
    value = loaded.get(key)
    if not isinstance(value, str):
        raise ValueError(f"runner field {key!r} must be a string in {where}")
    return value.strip() or default


def _arg_value_matches_type(value: Any, type_str: str) -> bool:
    if type_str == "string":
        return isinstance(value, str)
    if type_str == "integer":
        # Reject bool: in Python bool is a subclass of int but not what authors mean.
        return isinstance(value, int) and not isinstance(value, bool)
    if type_str == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_str == "boolean":
        return isinstance(value, bool)
    return False


__all__ = [
    "RunnerArg",
    "RunnerSpec",
    "RunnersDoc",
    "RunnerLifetime",
    "SUPPORTED_ARG_TYPES",
    "SUPPORTED_INTERPRETERS",
    "SUPPORTED_NETWORK_MODES",
    "SUPPORTED_LIFETIME_VALUES",
    "parse_runners_yaml",
    "parse_runners_yaml_text",
]
