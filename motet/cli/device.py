"""
Motet - Device CLI Re-export

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-03-26

Description:
    Runtime package re-export for the device CLI group implemented in motet_sdk.

Dependencies:
    - motet_sdk.cli.device: source CLI implementation

Usage:
    from motet.cli.device import device_group
"""
from motet_sdk.cli.device import device_group

__all__ = ["device_group"]
