"""
Intentionally malformed command for negative lint / validate tests (ADR-0071).

This file is deliberately broken in two ways that the bundle validator must catch:
  1. Syntax error: missing closing parenthesis on the decorator.
  2. No docstring on the decorated function (lint rule: every @distributed_command
     must have a docstring describing its purpose).

The validate endpoint MUST return lint_error events for this file and MUST NOT
register the command in any worker registry.
"""
from motet.core.commands.decorator import distributed_command

# Intentional syntax error — the decorator call is never closed.
@distributed_command(timeout_seconds=30
def broken(data):
    pass
