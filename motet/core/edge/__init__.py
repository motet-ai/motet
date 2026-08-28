"""
Motet - Edge worker package.

Device registry, HTTP vault client, and MCP config filter for edge
workers connected via WireGuard tunnel.
"""

from motet.core.edge.device_registry import DeviceAuthSession, DeviceRecord, EdgeDeviceRegistry
from motet.core.edge.http_vault_client import HttpVaultClient

__all__ = [
    "DeviceAuthSession",
    "DeviceRecord",
    "HttpVaultClient",
    "EdgeDeviceRegistry",
]
