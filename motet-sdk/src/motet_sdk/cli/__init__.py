"""
Motet SDK - Full CLI (bundle, local, commands, deploy, chat, etc.).

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

The full motet-cli lives here. When running from the repo, motet is
on the path and all groups (commands, chat, deploy, etc.) work. Entry point:
motet-cli = motet_sdk.cli:main
"""

from .main import main_group

# Entry point: main is the click group to invoke
main = main_group

__all__ = [
    "main",
    "main_group",
]
