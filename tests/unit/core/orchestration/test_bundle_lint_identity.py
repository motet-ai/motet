"""
Motet - Bundle Lint Identity Hygiene Tests (ADR-0090)

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-03-18

Description:
    Unit tests for the three identity-hygiene lint rules added to
    _lint_python_file in deploy.py (ADR-0090):
      1. @motet.tool functions must not accept a MotetContext parameter.
      2. No hardcoded "system:*" principal strings in bundle code.
      3. Ad-hoc stack identity access should use resolve_current_identity.

Dependencies:
    - pytest
    - motet.core.bundles.deploy: _lint_python_file

Usage:
    pytest tests/unit/core/orchestration/test_bundle_lint_identity.py -v

Notes:
    - All tests are pure unit tests — no Docker, Redis, or network required.
    - Each test feeds synthetic Python source to _lint_python_file and
      asserts the expected lint errors/warnings are produced (or not).
"""

import textwrap

import pytest

from motet.core.bundles.deploy import _lint_python_file


class TestToolMotetContextParameter:
    """Rule: @motet.tool functions must not accept a MotetContext parameter."""

    def test_motet_tool_with_motet_context_param_errors(self):
        src = textwrap.dedent("""\
            from motet_sdk import motet, MotetContext

            @motet.tool(description="bad tool", name="bad")
            def bad_tool(params, motet: MotetContext):
                pass
        """)
        errors = _lint_python_file("tools/bad.py", src)
        tool_errors = [e for e in errors if "MotetContext parameter" in e.message]
        assert len(tool_errors) == 1
        assert tool_errors[0].severity == "error"
        assert "bad_tool" in tool_errors[0].message

    def test_motet_tool_without_motet_context_is_clean(self):
        src = textwrap.dedent("""\
            from motet_sdk import motet

            @motet.tool(description="good tool", name="good")
            def good_tool(params):
                pass
        """)
        errors = _lint_python_file("tools/good.py", src)
        tool_errors = [e for e in errors if "MotetContext parameter" in e.message]
        assert len(tool_errors) == 0

    def test_command_with_motet_context_is_allowed(self):
        """@motet.command functions are supposed to have MotetContext."""
        src = textwrap.dedent("""\
            from motet_sdk import motet, MotetContext

            @motet.command(timeout_seconds=60)
            def my_cmd(data, motet: MotetContext):
                pass
        """)
        errors = _lint_python_file("commands/my_cmd.py", src)
        tool_errors = [e for e in errors if "MotetContext parameter" in e.message]
        assert len(tool_errors) == 0

    def test_bare_motet_tool_decorator(self):
        """@motet.tool without call parens (unlikely but valid AST)."""
        src = textwrap.dedent("""\
            from motet_sdk import motet, MotetContext

            @motet.tool
            def bad_tool(params, ctx: MotetContext):
                pass
        """)
        errors = _lint_python_file("tools/bare.py", src)
        tool_errors = [e for e in errors if "MotetContext parameter" in e.message]
        assert len(tool_errors) == 1


class TestHardcodedSystemPrincipal:
    """Rule: no hardcoded 'system:*' principal strings in bundle code."""

    def test_hardcoded_system_principal_warns(self):
        src = textwrap.dedent("""\
            PRINCIPAL = "system:my-service"
        """)
        errors = _lint_python_file("tools/bad.py", src)
        sys_errors = [e for e in errors if "system:*" in e.message]
        assert len(sys_errors) == 1
        assert sys_errors[0].severity == "warning"

    def test_system_principal_in_comment_is_ignored(self):
        src = textwrap.dedent("""\
            # This uses "system:scheduler" as the principal
            x = 1
        """)
        errors = _lint_python_file("tools/ok.py", src)
        sys_errors = [e for e in errors if "system:*" in e.message]
        assert len(sys_errors) == 0

    def test_no_system_principal_is_clean(self):
        src = textwrap.dedent("""\
            from motet_sdk import IdentityContext
            ID = IdentityContext(tenant_id="t1", motet_id="m1", principal_id="user-1")
        """)
        errors = _lint_python_file("tools/clean.py", src)
        sys_errors = [e for e in errors if "system:*" in e.message]
        assert len(sys_errors) == 0

    def test_system_principal_with_single_quotes_warns(self):
        src = textwrap.dedent("""\
            PRINCIPAL = 'system:vault-client'
        """)
        errors = _lint_python_file("commands/bad.py", src)
        sys_errors = [e for e in errors if "system:*" in e.message]
        assert len(sys_errors) == 1


class TestAdhocStackIdentityAccess:
    """Rule: ad-hoc stack identity access should use resolve_current_identity."""

    def test_stack_principal_id_in_tool_warns(self):
        src = textwrap.dedent("""\
            def my_tool(params):
                pid = stack._principal_id
        """)
        errors = _lint_python_file("tools/bad_tool.py", src)
        adhoc_errors = [e for e in errors if "Ad-hoc stack identity" in e.message]
        assert len(adhoc_errors) == 1
        assert adhoc_errors[0].severity == "warning"

    def test_getattr_stack_tenant_in_tool_warns(self):
        src = textwrap.dedent("""\
            def my_tool(params):
                tid = getattr(stack, "_tenant_id", "")
        """)
        errors = _lint_python_file("tools/bad_tool2.py", src)
        adhoc_errors = [e for e in errors if "Ad-hoc stack identity" in e.message]
        assert len(adhoc_errors) == 1

    def test_stack_identity_in_command_is_not_flagged(self):
        """Ad-hoc check only applies to tools/ files."""
        src = textwrap.dedent("""\
            def my_cmd(data, motet):
                pid = stack._principal_id
        """)
        errors = _lint_python_file("commands/my_cmd.py", src)
        adhoc_errors = [e for e in errors if "Ad-hoc stack identity" in e.message]
        assert len(adhoc_errors) == 0

    def test_resolve_current_identity_is_clean(self):
        src = textwrap.dedent("""\
            from motet_sdk import resolve_current_identity

            def my_tool(params):
                identity = resolve_current_identity()
                return identity.principal_id
        """)
        errors = _lint_python_file("tools/good_tool.py", src)
        adhoc_errors = [e for e in errors if "Ad-hoc stack identity" in e.message]
        assert len(adhoc_errors) == 0

    def test_stack_identity_in_comment_is_ignored(self):
        src = textwrap.dedent("""\
            # stack._principal_id is deprecated, use resolve_current_identity
            def my_tool(params):
                pass
        """)
        errors = _lint_python_file("tools/commented.py", src)
        adhoc_errors = [e for e in errors if "Ad-hoc stack identity" in e.message]
        assert len(adhoc_errors) == 0
