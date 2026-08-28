"""
Motet - MCP Transport Factory

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Registry-based factory pattern for creating MCP transport instances in the Motet
    distributed framework. Provides extensible transport creation based on configuration
    with support for custom transport registration. Includes comprehensive transport
    lifecycle management and error handling.

Dependencies:
    - typing: Type hints and annotations
    - structlog: Structured logging and observability
    - Base transport interfaces and implementations

Usage:
    from motet.core.tools.mcp_motet.transports.factory import MCPTransportFactory

    # Register custom transport
    MCPTransportFactory.register_transport("custom", CustomMCPTransport)

    # Create transport
    transport = MCPTransportFactory.create_transport("stdio", config)

    # Get available transports
    transports = MCPTransportFactory.get_available_transports()

Notes:
    - Provides registry-based factory pattern for transport creation
    - Supports extensible transport registration and discovery
    - Includes comprehensive transport lifecycle management
    - Supports configuration-based transport creation
    - Includes error handling and validation
    - Integrates with MCP transport system
    - Includes comprehensive logging and observability
"""

from typing import Dict, Any, Optional, Type
import structlog

from motet.core.tools.mcp_motet.transports.base import MCPTransport

logger = structlog.get_logger(__name__)


class MCPTransportFactory:
    """
    Factory for creating MCP transport instances.
    
    Uses a registry pattern to map transport types (strings) to transport classes.
    This allows for easy extensibility - new transports can be registered without
    modifying the factory code.
    
    Example usage:
        # Register a custom transport
        MCPTransportFactory.register_transport("custom", CustomMCPTransport)
        
        # Create a transport instance
        transport = MCPTransportFactory.create_transport(
            transport_type="stdio",
            service_id="weather",
            config={"command": "npx", "args": ["-y", "@timlukahorstmann/mcp-weather"]}
        )
    """
    
    # Registry mapping transport type names to transport classes
    _transport_types: Dict[str, Type[MCPTransport]] = {}
    
    @classmethod
    def register_transport(
        cls,
        transport_type: str,
        transport_class: Type[MCPTransport]
    ) -> None:
        """
        Register a transport type.
        
        Args:
            transport_type: String identifier for the transport (e.g., "stdio", "http")
            transport_class: Transport class (must inherit from MCPTransport)
            
        Raises:
            TypeError: If transport_class doesn't inherit from MCPTransport
        """
        if not issubclass(transport_class, MCPTransport):
            raise TypeError(
                f"Transport class {transport_class.__name__} must inherit from MCPTransport"
            )
        
        if transport_type in cls._transport_types:
            logger.warning(
                "Overwriting existing transport registration",
                transport_type=transport_type,
                old_class=cls._transport_types[transport_type].__name__,
                new_class=transport_class.__name__
            )
        
        cls._transport_types[transport_type] = transport_class
        logger.debug(
            "Registered transport type",
            transport_type=transport_type,
            transport_class=transport_class.__name__
        )
    
    @classmethod
    def create_transport(
        cls,
        transport_type: str,
        service_id: str,
        config: Dict[str, Any],
        worker_id: Optional[str] = None,
        startup_command_context: Optional[Any] = None
    ) -> MCPTransport:
        """
        Create a transport instance based on type.
        
        Args:
            transport_type: Type of transport to create (e.g., "stdio", "http")
            service_id: Unique identifier for the MCP service
            config: Transport-specific configuration
            worker_id: Optional worker ID for distributed environments
            startup_command_context: Optional context for vault credential fetching
            
        Returns:
            Configured transport instance
            
        Raises:
            ValueError: If transport_type is not registered
        """
        # Normalize transport type (lowercase, handle aliases)
        transport_type = transport_type.lower()
        
        # Handle common aliases
        alias_map = {
            "streamable-http": "http",
            "streamable_http": "http",
            "subprocess": "stdio",
        }
        transport_type = alias_map.get(transport_type, transport_type)
        
        # Look up transport class
        transport_class = cls._transport_types.get(transport_type)
        
        if not transport_class:
            available = ", ".join(sorted(cls._transport_types.keys()))
            raise ValueError(
                f"Unknown transport type: '{transport_type}'. "
                f"Available types: {available}. "
                f"Register new transports with MCPTransportFactory.register_transport()"
            )
        
        # Create and return transport instance
        logger.info(
            "Creating transport instance",
            transport_type=transport_type,
            transport_class=transport_class.__name__,
            service_id=service_id,
            worker_id=worker_id
        )
        
        return transport_class(
            service_id=service_id,
            config=config,
            worker_id=worker_id,
            startup_command_context=startup_command_context
        )
    
    @classmethod
    def get_registered_transports(cls) -> Dict[str, Type[MCPTransport]]:
        """
        Get all registered transport types.
        
        Returns:
            Dictionary mapping transport type names to transport classes
        """
        return cls._transport_types.copy()
    
    @classmethod
    def is_registered(cls, transport_type: str) -> bool:
        """
        Check if a transport type is registered.
        
        Args:
            transport_type: Transport type to check
            
        Returns:
            True if registered, False otherwise
        """
        transport_type = transport_type.lower()
        alias_map = {
            "streamable-http": "http",
            "streamable_http": "http",
            "subprocess": "stdio",
        }
        transport_type = alias_map.get(transport_type, transport_type)
        return transport_type in cls._transport_types


def _auto_register_builtin_transports():
    """
    Auto-register built-in transport types.
    
    This is called at module import time to register the stdio and http transports.
    Gracefully handles missing transport implementations during development.
    """
    try:
        from motet.core.tools.mcp_motet.transports.stdio import StdioMCPTransport
        MCPTransportFactory.register_transport("stdio", StdioMCPTransport)
    except ImportError:
        logger.debug("StdioMCPTransport not yet implemented, skipping registration")
    
    try:
        from motet.core.tools.mcp_motet.transports.http import HTTPMCPTransport
        MCPTransportFactory.register_transport("http", HTTPMCPTransport)
    except ImportError:
        logger.debug("HTTPMCPTransport not yet implemented, skipping registration")


# Auto-register built-in transports on module import
_auto_register_builtin_transports()

