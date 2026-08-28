"""
Unit tests for ``motet.core.skills.runners`` (ADR-0101 Slice B).

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-04-25

Description:
    Exercises the strict parser contract for ``runners.yaml``: required
    keys, type checks, default backfill, name slug rules, args
    validation, and lifetime parsing. These tests pin the schema contract
    that the runtime relies on so authors get the same errors at lint time
    as at deploy time.
"""

from __future__ import annotations

import pytest

from motet.core.skills.runners import (
    SUPPORTED_INTERPRETERS,
    SUPPORTED_NETWORK_MODES,
    SUPPORTED_LIFETIME_VALUES,
    parse_runners_yaml_text,
)


def test_parses_minimal_runner_with_defaults() -> None:
    text = """
runners:
  - name: echo
    description: Hello world.
    script: scripts/echo.py
"""
    doc = parse_runners_yaml_text(text, source_hint="t.yaml")
    assert len(doc.runners) == 1
    r = doc.runners[0]
    assert r.name == "echo"
    assert r.script == "scripts/echo.py"
    assert r.interpreter == "python3"
    assert r.image_stack == "python-minimal"
    assert r.lifetime == "ephemeral"
    assert r.network == "inherit"
    assert r.timeout_seconds is None
    assert r.credentials == ()
    assert r.args == ()


def test_empty_or_missing_runners_is_valid() -> None:
    assert parse_runners_yaml_text("", source_hint="x").runners == ()
    assert parse_runners_yaml_text("runners: []", source_hint="x").runners == ()


def test_top_level_must_be_mapping() -> None:
    with pytest.raises(ValueError, match="top-level must be a mapping"):
        parse_runners_yaml_text("- not a mapping\n", source_hint="x.yaml")


def test_runners_must_be_list() -> None:
    with pytest.raises(ValueError, match="'runners' must be a list"):
        parse_runners_yaml_text("runners: {}\n", source_hint="x.yaml")


def test_runner_entry_must_be_mapping() -> None:
    with pytest.raises(ValueError, match=r"runners\[0\] must be a mapping"):
        parse_runners_yaml_text("runners:\n  - not-a-mapping\n", source_hint="x.yaml")


def test_required_fields_enforced() -> None:
    text = "runners:\n  - description: missing name\n    script: s.py\n"
    with pytest.raises(ValueError, match="missing required field 'name'"):
        parse_runners_yaml_text(text, source_hint="x.yaml")


def test_runner_name_must_be_slug() -> None:
    text = """
runners:
  - name: BadName
    description: x
    script: s.py
"""
    with pytest.raises(ValueError, match="must use lowercase letters"):
        parse_runners_yaml_text(text, source_hint="x.yaml")


def test_duplicate_runner_names_rejected() -> None:
    text = """
runners:
  - name: a
    description: x
    script: s.py
  - name: a
    description: y
    script: t.py
"""
    with pytest.raises(ValueError, match="duplicate runner name 'a'"):
        parse_runners_yaml_text(text, source_hint="x.yaml")


def test_script_must_be_relative_and_no_parent_traversal() -> None:
    for bad in ("/abs/path.py", "../escape.py", "scripts/../escape.py"):
        text = f"""
runners:
  - name: r
    description: x
    script: {bad}
"""
        with pytest.raises(ValueError, match="must be a bundle-relative path"):
            parse_runners_yaml_text(text, source_hint="x.yaml")


@pytest.mark.parametrize("interpreter", list(SUPPORTED_INTERPRETERS))
def test_supported_interpreters_accepted(interpreter: str) -> None:
    text = f"""
runners:
  - name: r
    description: x
    script: s
    interpreter: {interpreter}
"""
    assert parse_runners_yaml_text(text, "x").runners[0].interpreter == interpreter


def test_unknown_interpreter_rejected() -> None:
    text = """
runners:
  - name: r
    description: x
    script: s
    interpreter: ruby
"""
    with pytest.raises(ValueError, match="'interpreter' must be one of"):
        parse_runners_yaml_text(text, "x.yaml")


@pytest.mark.parametrize("lifetime", list(SUPPORTED_LIFETIME_VALUES))
def test_supported_lifetime_strings_accepted(lifetime: str) -> None:
    text = f"""
runners:
  - name: r
    description: x
    script: s
    lifetime: {lifetime}
"""
    assert parse_runners_yaml_text(text, "x").runners[0].lifetime == lifetime


def test_yaml_bool_lifetime_false_normalized() -> None:
    # YAML's `false` decodes as a bool; parser maps it to the default lifetime.
    text = """
runners:
  - name: r
    description: x
    script: s
    lifetime: false
"""
    assert parse_runners_yaml_text(text, "x").runners[0].lifetime == "ephemeral"


def test_unknown_lifetime_rejected() -> None:
    text = """
runners:
  - name: r
    description: x
    script: s
    lifetime: cold
"""
    with pytest.raises(ValueError, match="'lifetime' must be one of"):
        parse_runners_yaml_text(text, "x.yaml")


def test_legacy_session_field_rejected() -> None:
    text = """
runners:
  - name: r
    description: x
    script: s
    session: cold
"""
    with pytest.raises(ValueError, match="field 'session' is no longer supported"):
        parse_runners_yaml_text(text, "x.yaml")


def test_timeout_must_be_positive_int_with_ceiling() -> None:
    bad_timeouts = ("'forty'", "0", "-1", "true", "999999")
    for raw in bad_timeouts:
        text = f"""
runners:
  - name: r
    description: x
    script: s
    timeout_seconds: {raw}
"""
        with pytest.raises(ValueError, match="'timeout_seconds'"):
            parse_runners_yaml_text(text, "x.yaml")


@pytest.mark.parametrize("net", list(SUPPORTED_NETWORK_MODES))
def test_network_modes(net: str) -> None:
    text = f"""
runners:
  - name: r
    description: x
    script: s
    network: {net}
"""
    assert parse_runners_yaml_text(text, "x").runners[0].network == net


def test_credentials_must_be_unique_strings() -> None:
    bad = """
runners:
  - name: r
    description: x
    script: s
    credentials:
      - github
      - github
"""
    with pytest.raises(ValueError, match="duplicate entry 'github'"):
        parse_runners_yaml_text(bad, "x.yaml")


def test_args_required_default_and_type_checks() -> None:
    text = """
runners:
  - name: r
    description: x
    script: s
    args:
      text:
        type: string
        description: hello
        default: "x"
      count:
        type: integer
        required: true
"""
    runner = parse_runners_yaml_text(text, "x.yaml").runners[0]
    by_name = {a.name: a for a in runner.args}
    assert by_name["text"].type == "string"
    assert by_name["text"].default == "x"
    assert by_name["count"].required is True
    assert by_name["count"].default is None


def test_args_default_must_match_declared_type() -> None:
    text = """
runners:
  - name: r
    description: x
    script: s
    args:
      count:
        type: integer
        default: "ten"
"""
    with pytest.raises(ValueError, match="default does not match declared type"):
        parse_runners_yaml_text(text, "x.yaml")


def test_args_unknown_type_rejected() -> None:
    text = """
runners:
  - name: r
    description: x
    script: s
    args:
      x:
        type: object
"""
    with pytest.raises(ValueError, match="'type' must be one of"):
        parse_runners_yaml_text(text, "x.yaml")


def test_tool_name_composition() -> None:
    text = """
runners:
  - name: echo
    description: x
    script: s.py
"""
    runner = parse_runners_yaml_text(text, "x").runners[0]
    assert runner.tool_name("acme.demo", "writer") == "acme.demo.writer.echo"


def test_invalid_yaml_surfaces_clear_error() -> None:
    with pytest.raises(ValueError, match="Invalid YAML"):
        parse_runners_yaml_text("runners:\n  - name: a\n  description: bad-indent\n", "x.yaml")
