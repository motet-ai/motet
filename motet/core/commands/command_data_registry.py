"""
Motet - Command Data Registry

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-22

Description:
    Thread-safe registry for command data classes in the Motet distributed framework.
    Provides centralized registration and retrieval of command data classes using a
    self-registration pattern to avoid circular imports. Includes lazy loading support,
    duplicate registration detection, and comprehensive error handling.

Dependencies:
    - pydantic: Data validation and model definitions
    - structlog: Structured logging
    - typing: Type hints and annotations
    - motet.core.registry: ScopedRegistry and scope metadata primitives
    - motet.core.workers.concurrency_primitives: WorkerRLock for pool-safe lazy loader sync

Usage:
    from motet.core.commands.command_data_registry import command_data_registry
    
    # Register command data class
    command_data_registry.register("tool_execution", ToolExecutionData)
    
    # Get command data class
    data_class = command_data_registry.get("tool_execution")
    
    # Create data instance
    data_instance = data_class(field="value")

Notes:
    - Provides thread-safe registry for command data classes
    - Supports self-registration pattern to avoid circular imports
    - Includes lazy loading support for dynamic imports
    - Provides duplicate registration detection and error handling
    - Supports comprehensive command data class management
    - Integrates with distributed command system
    - Includes clear error messages for missing registrations
"""


from typing import Any, Callable, Dict, List, Optional, Type
from pydantic import BaseModel
import structlog
from ..registry import ScopedRegistry, RegistryScope, scope_from_qualified_name
from ..workers.concurrency_primitives import WorkerRLock

logger = structlog.get_logger(__name__)

# Type alias for command data classes
CommandDataClass = Type[BaseModel]


class CommandDataRegistry(ScopedRegistry[CommandDataClass]):
    """
    Thread-safe registry for command data classes.
    
    This registry uses a self-registration pattern to avoid circular imports:
    - Data class modules register themselves on import
    - Registry never imports data class modules
    - Supports lazy loading fallbacks for unregistered types
    
    Features:
    - Thread-safe with RLock
    - Lazy loading support for dynamic imports
    - Duplicate registration detection
    - Clear error messages for missing registrations
    
    Example:
        >>> # In data class module
        >>> class MyCommandData(BaseCommandData):
        ...     field: str
        >>> 
        >>> command_data_registry.register("my_command", MyCommandData)
        >>> 
        >>> # Later, in executor
        >>> data_class = command_data_registry.get("my_command")
        >>> data_instance = data_class(field="value")
    """
    
    def __init__(self):
        super().__init__(registry_name="command_data_registry")
        self._lazy_lock = WorkerRLock()
        self._lazy_loaders: Dict[str, Callable[[], CommandDataClass]] = {}
        
    def register(
        self,
        key: Optional[str] = None,
        item: Optional[CommandDataClass] = None,
        *,
        scope: Optional[RegistryScope] = None,
        metadata: Optional[Dict[str, Any]] = None,
        overwrite: bool = False,
        command_type: Optional[str] = None,
        data_class: Optional[CommandDataClass] = None,
    ) -> None:
        """
        Register a command data class.
        
        Args:
            key: Unique command type identifier (e.g., "tool_execution")
            item: Pydantic BaseModel class for this command type
            scope: Optional registry scope (computed from key when omitted)
            metadata: Optional entry metadata forwarded to ScopedRegistry
            overwrite: Allow overwriting existing registration (default: False)
            command_type: Backward-compatible alias for key
            data_class: Backward-compatible alias for item
            
        Raises:
            ValueError: If key already registered with different class and overwrite=False
            
        Example:
            >>> command_data_registry.register("tool_execution", ToolExecutionData)
        """
        if key is None:
            key = command_type
        elif command_type is not None and command_type != key:
            raise ValueError("register received conflicting values for 'key' and 'command_type'")

        if item is None:
            item = data_class
        elif data_class is not None and data_class != item:
            raise ValueError("register received conflicting values for 'item' and 'data_class'")

        if key is None:
            raise TypeError("register() missing required argument: 'key' (or legacy 'command_type')")
        if item is None:
            raise TypeError("register() missing required argument: 'item' (or legacy 'data_class')")

        command_type = key
        data_class = item
        with self._lazy_lock:
            existing = super().get(command_type)
            if existing is not None and not overwrite:
                if existing != data_class:
                    raise ValueError(
                        f"Command type '{command_type}' already registered "
                        f"with {existing.__name__}, cannot register {data_class.__name__}. "
                        f"Use overwrite=True to force re-registration."
                    )
                # Already registered with same class, no-op
                return
            eff_scope = scope
            if eff_scope is None:
                eff_scope = scope_from_qualified_name(command_type)
            super().register(
                command_type,
                data_class,
                scope=eff_scope,
                metadata=dict(metadata or {}),
            )
            logger.debug(
                "Registered command data class",
                command_type=command_type,
                data_class=data_class.__name__,
                overwrite=overwrite
            )
    
    def register_lazy(
        self,
        command_type: str,
        loader: Callable[[], CommandDataClass]
    ) -> None:
        """
        Register a lazy loader for a command type.
        
        The loader will be called only if the command type is not already
        registered when get() is called. Useful for plugin commands or
        large modules that should only be imported if needed.
        
        Args:
            command_type: Unique command type identifier
            loader: Callable that returns the data class when invoked
            
        Example:
            >>> def load_agent_data():
            ...     from ...reasoning.react.agent_data import AgentData
            ...     return AgentData
            >>> 
            >>> command_data_registry.register_lazy("agent", load_agent_data)
        """
        with self._lazy_lock:
            self._lazy_loaders[command_type] = loader
            logger.debug(
                "Registered lazy loader for command data class",
                command_type=command_type
            )
    
    def get(self, key: Optional[str] = None, *, command_type: Optional[str] = None) -> Optional[CommandDataClass]:
        """
        Get a command data class by type.
        
        Args:
            key: Command type identifier
            command_type: Backward-compatible alias for key
            
        Returns:
            Command data class or None if not found
            
        Example:
            >>> data_class = command_data_registry.get("tool_execution")
            >>> if data_class:
            ...     instance = data_class(tool_name="test", parameters={})
        """
        if key is None:
            key = command_type
        elif command_type is not None and command_type != key:
            raise ValueError("get received conflicting values for 'key' and 'command_type'")
        if key is None:
            raise TypeError("get() missing required argument: 'key' (or legacy 'command_type')")

        command_type = key
        data_class = super().get(command_type)
        if data_class is not None:
            return data_class
            
        with self._lazy_lock:
            # Double-check after lock acquisition to avoid duplicate loads.
            data_class = super().get(command_type)
            if data_class is not None:
                return data_class

            if command_type in self._lazy_loaders:
                try:
                    loader = self._lazy_loaders[command_type]
                    data_class = loader()
                    # Cache the loaded class
                    super().register(
                        command_type,
                        data_class,
                        scope=scope_from_qualified_name(command_type),
                        metadata={},
                    )
                    # Remove loader after successful load
                    del self._lazy_loaders[command_type]
                    logger.info(
                        "Lazy loaded command data class",
                        command_type=command_type,
                        data_class=data_class.__name__
                    )
                    return data_class
                except Exception as e:
                    logger.error(
                        "Failed to lazy load command data class",
                        command_type=command_type,
                        error=str(e),
                        exc_info=True
                    )
                    return None

            logger.debug(
                "Command data class not found",
                command_type=command_type,
                available_types=list(self.list_items().keys())[:10]  # Show first 10
            )
            return None
    
    def get_all(self) -> Dict[str, CommandDataClass]:
        """
        Get all registered command data classes.
        
        Note: Does not trigger lazy loaders. Only returns eagerly registered classes.
        
        Returns:
            Dictionary mapping command types to their data classes
            
        Example:
            >>> all_classes = command_data_registry.get_all()
            >>> print(f"Registered: {len(all_classes)} command data classes")
        """
        return self.list_items()
    
    def get_types(self) -> List[str]:
        """
        Get all registered command types.
        
        Note: Does not trigger lazy loaders. Only returns eagerly registered types.
        
        Returns:
            List of command type strings, sorted alphabetically
            
        Example:
            >>> types = command_data_registry.get_types()
            >>> print(f"Available types: {', '.join(types[:5])}")
        """
        return sorted(self.list_items().keys())
    
    def is_registered(self, command_type: str) -> bool:
        """
        Check if a command type is registered.
        
        Args:
            command_type: Command type identifier
            
        Returns:
            True if registered (eagerly or lazily), False otherwise
            
        Example:
            >>> if command_data_registry.is_registered("tool_execution"):
            ...     print("Tool execution is available")
        """
        with self._lazy_lock:
            return (
                super().get(command_type) is not None or
                command_type in self._lazy_loaders
            )
    
    def unregister(self, key: Optional[str] = None, *, command_type: Optional[str] = None) -> bool:
        """
        Unregister a command type.
        
        Useful for testing and hot-reloading scenarios.
        
        Args:
            key: Command type identifier
            command_type: Backward-compatible alias for key
            
        Returns:
            True if unregistered, False if not found
            
        Example:
            >>> # For testing
            >>> command_data_registry.unregister("my_test_command")
        """
        if key is None:
            key = command_type
        elif command_type is not None and command_type != key:
            raise ValueError("unregister received conflicting values for 'key' and 'command_type'")
        if key is None:
            raise TypeError("unregister() missing required argument: 'key' (or legacy 'command_type')")

        command_type = key
        with self._lazy_lock:
            removed = False
            if super().unregister(command_type):
                removed = True
                logger.debug(
                    "Unregistered command data class",
                    command_type=command_type
                )
            if command_type in self._lazy_loaders:
                del self._lazy_loaders[command_type]
                removed = True
                logger.debug(
                    "Unregistered lazy loader for command data class",
                    command_type=command_type
                )
            return removed
    
    def clear(self) -> None:
        """
        Clear all registrations.
        
        Use with caution! This will remove all registered command data classes.
        Primarily for testing purposes.
        
        Example:
            >>> # In test teardown
            >>> command_data_registry.clear()
        """
        with self._lazy_lock:
            count = len(self.list_items())
            for command_type in list(self.list_items().keys()):
                super().unregister(command_type)
            self._lazy_loaders.clear()
            logger.warning(
                "Cleared command data registry",
                cleared_count=count
            )
    
    def get_stats(self) -> Dict[str, int]:
        """
        Get registry statistics.
        
        Returns:
            Dictionary with 'registered' and 'lazy_loaders' counts
            
        Example:
            >>> stats = command_data_registry.get_stats()
            >>> print(f"Registered: {stats['registered']}, Lazy: {stats['lazy_loaders']}")
        """
        return {"registered": len(self.list_items()), "lazy_loaders": len(self._lazy_loaders)}


# Global singleton registry instance
command_data_registry = CommandDataRegistry()


# Convenience functions for backward compatibility
def register_command_data(
    command_type: str, 
    data_class: CommandDataClass,
    *,
    overwrite: bool = False
) -> None:
    """
    Register a command data class (convenience function).
    
    Args:
        command_type: Unique command type identifier
        data_class: Pydantic BaseModel class
        overwrite: Allow overwriting existing registration
    """
    command_data_registry.register(command_type, data_class, overwrite=overwrite)


def get_command_data_class(command_type: str) -> Optional[CommandDataClass]:
    """
    Get a command data class (convenience function).
    
    Args:
        command_type: Command type identifier
        
    Returns:
        Command data class or None if not found
    """
    return command_data_registry.get(command_type)


def get_all_command_data_classes() -> Dict[str, CommandDataClass]:
    """Get all command data classes (convenience function)."""
    return command_data_registry.get_all()


def get_command_types() -> List[str]:
    """Get all command types (convenience function)."""
    return command_data_registry.get_types()

