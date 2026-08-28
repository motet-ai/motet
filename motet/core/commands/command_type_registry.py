"""
Motet - Command Type Registry

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Unified registry for all command types in the Motet distributed framework.
    Extends ``ScopedRegistry`` for grant-based visibility,
    namespace lifecycle, and pool-agnostic locking, while retaining command-specific
    versioning, implementation typing, and ``instantiate_command``.

    Each registration carries a first-class ``description`` (same role as
    ``RegisteredTool.description``) used by function discovery / ``core.help`` (#194).
    Callers may pass it explicitly; otherwise ``register_command`` derives it from the
    implementation docstring (decorator ``_original_function`` or command class) and
    falls back to the data-class docstring.

Dependencies:
    - motet.core.registry: ScopedRegistry, RegistryScope, ScopeFilter, RegistryEntry
    - motet.core.workers.concurrency_primitives: WorkerRLock for singleton init
    - enum: Command implementation type enumeration
    - pydantic: Data validation and model definitions
    - structlog: Structured logging
    - typing: Type hints and annotations

Usage:
    from motet.core.commands.command_type_registry import command_type_registry, CommandImplementationType

    # Register decorator-based command
    command_type_registry.register_command(
        command_type="tool_execution",
        implementation=tool_execution,
        implementation_type=CommandImplementationType.DECORATOR_BASED
    )

    # Get command registration
    registration = command_type_registry.get("tool_execution")

Notes:
    - Subclasses ScopedRegistry[CommandRegistration]; storage lives in ``_entries``
    - Domain extras: ``_versions``, ``get_stats()`` implementation-type counters
    - Bundle-sourced commands use DECORATOR_BASED with hot_loadable=True and bundle_id set
    - ``description`` is first-class discovery prose; do not rely on the vector store
      to scrape DecoratedCommand / data-class docstrings
    - ``unregister_namespace`` clears versions/stats without nested lock acquire
"""

from __future__ import annotations

from enum import Enum
from typing import ClassVar, Dict, Any, Optional, List, Union, Type, Callable
import structlog
from pydantic import BaseModel, Field, model_validator

from ..registry import (
    RegistryScope,
    RegistryEntry,
    ScopedRegistry,
    scope_from_qualified_name,
    normalize_namespace,
    namespace_to_bundle_id,
)
from ..workers.concurrency_primitives import WorkerRLock

logger = structlog.get_logger(__name__)


class CommandImplementationType(Enum):
    """Type of command implementation."""
    CLASS_BASED = "class"          # Traditional DistributedCommand subclass
    DECORATOR_BASED = "decorator"  # @distributed_command function (includes bundle-sourced commands)


# Placeholder written onto every decorator-generated DistributedCommand subclass.
# Prefer ``_original_function.__doc__`` / an explicit registration description.
_PLACEHOLDER_COMMAND_DOCSTRINGS = frozenset(
    {
        "Dynamically generated command from decorated function.",
    }
)


def first_docstring_line(obj: Any) -> str:
    """Return the first non-empty line of ``obj.__doc__``, or empty string."""
    doc = getattr(obj, "__doc__", None)
    if not doc:
        return ""
    for line in str(doc).strip().splitlines():
        text = line.strip()
        if text:
            return text
    return ""


def derive_command_description(
    implementation: Any,
    data_class: Optional[Type[BaseModel]] = None,
) -> str:
    """
    Derive discovery prose for a command implementation (#194).

    Order: decorator ``_original_function`` docstring, implementation docstring
    (skipping the generated-class placeholder), then data-class docstring.
    """
    if implementation is not None:
        original = getattr(implementation, "_original_function", None)
        if original is not None:
            if isinstance(original, staticmethod):
                original = original.__func__
            line = first_docstring_line(original)
            if line and line not in _PLACEHOLDER_COMMAND_DOCSTRINGS:
                return line

        line = first_docstring_line(implementation)
        if line and line not in _PLACEHOLDER_COMMAND_DOCSTRINGS:
            return line

    if data_class is not None:
        return first_docstring_line(data_class)
    return ""


class CommandRegistration(BaseModel):
    """
    Registration entry for a command.

    Stores metadata about a registered command including its implementation,
    type, version, capabilities, and discovery description.
    """
    command_type: str
    implementation_type: CommandImplementationType
    implementation: Union[Type, Callable]  # Class or function
    data_class: Optional[Type[BaseModel]] = None
    description: str = ""  # First-class discovery prose (tool-parity; #194)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    version: str = "1.0.0"
    bundle_id: Optional[str] = None  # Bundle that contributed this command (manifest name)
    hot_loadable: bool = False  # True if dynamically loadable/unloadable (e.g. from bundle deploy)

    model_config = {"arbitrary_types_allowed": True}  # Allow Type and Callable types

    @model_validator(mode='after')
    def validate_registration(self) -> 'CommandRegistration':
        """Validate registration on creation."""
        if not self.command_type:
            raise ValueError("command_type is required")
        if not self.implementation:
            raise ValueError("implementation is required")
        if not isinstance(self.implementation_type, CommandImplementationType):
            raise ValueError("implementation_type must be CommandImplementationType enum")
        return self


class CommandTypeRegistry(ScopedRegistry[CommandRegistration]):
    """
    Unified registry for all command types (ADR-0079 ScopedRegistry).

    Supports:
    - Class-based DistributedCommand subclasses
    - Decorator-based functions (@distributed_command)
    - Bundle-sourced commands (DECORATOR_BASED with hot_loadable=True, bundle_id set)

    Note: Bundle-sourced commands use DECORATOR_BASED with hot_loadable=True and
    bundle_id set to the manifest name; they are loaded/unloaded via the bundle deploy pipeline.

    Thread-safe via ScopedRegistry WorkerLock; singleton process instance.

    Example:
        >>> from motet.core.commands.command_type_registry import command_type_registry
        >>>
        >>> # Register a decorator-based command
        >>> command_type_registry.register_command(
        ...     command_type="my_command",
        ...     implementation=my_command_function,
        ...     implementation_type=CommandImplementationType.DECORATOR_BASED,
        ...     metadata={"capabilities": ["model_inference"]}
        ... )
        >>>
        >>> # Look up command
        >>> registration = command_type_registry.get("my_command")
        >>> print(registration.implementation_type)
        CommandImplementationType.DECORATOR_BASED
    """

    _instance: ClassVar[Optional["CommandTypeRegistry"]] = None
    _singleton_lock: ClassVar[WorkerRLock] = WorkerRLock()

    def __new__(cls) -> "CommandTypeRegistry":
        """Ensure singleton pattern."""
        with cls._singleton_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
            return cls._instance

    def __init__(self) -> None:
        """Initialize ScopedRegistry storage once for the singleton."""
        if getattr(self, "_initialized", False):
            return
        super().__init__(registry_name="command_type_registry")
        self._versions: Dict[str, Dict[str, CommandRegistration]] = {}
        self._stats: Dict[str, int] = {
            "registered": 0,
            "class_based": 0,
            "decorator_based": 0,
        }
        self._initialized = True

    def _resolve_scope(
        self,
        command_type: str,
        bundle_id: Optional[str],
        scope: Optional[RegistryScope],
    ) -> RegistryScope:
        if scope is not None:
            return scope
        resolved_scope = scope_from_qualified_name(command_type)
        # Preserve historical behavior: explicit bundle_id overrides namespace ownership.
        if bundle_id is not None:
            bundle_namespace = normalize_namespace(bundle_id)
            return RegistryScope(
                namespace=bundle_namespace,
                bundle_id=namespace_to_bundle_id(bundle_namespace),
                grants=resolved_scope.grants,
            )
        return resolved_scope

    def _bump_impl_stats(self, implementation_type: CommandImplementationType, delta: int) -> None:
        if implementation_type == CommandImplementationType.CLASS_BASED:
            self._stats["class_based"] = max(0, self._stats["class_based"] + delta)
        elif implementation_type == CommandImplementationType.DECORATOR_BASED:
            self._stats["decorator_based"] = max(0, self._stats["decorator_based"] + delta)

    def _unregister_all_unlocked(self, command_type: str) -> bool:
        """Remove all versions of a command. Caller must hold ``self._lock``."""
        entry = self._entries.pop(command_type, None)
        if entry is None:
            return False
        registration = entry.item
        self._stats["registered"] = max(0, self._stats["registered"] - 1)
        self._bump_impl_stats(registration.implementation_type, -1)
        self._versions.pop(command_type, None)
        logger.debug("Unregistered command type (all versions)", command_type=command_type)
        return True

    def register_command(
        self,
        command_type: str,
        implementation: Union[Type, Callable],
        implementation_type: CommandImplementationType,
        data_class: Optional[Type[BaseModel]] = None,
        description: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        version: str = "1.0.0",
        bundle_id: Optional[str] = None,
        hot_loadable: bool = False,
        overwrite: bool = False,
        scope: Optional[RegistryScope] = None,
    ) -> None:
        """
        Register a command implementation.

        Args:
            command_type: Unique command type identifier
            implementation: Command class or decorated function
            implementation_type: Type of implementation (class or decorator)
            data_class: Associated data class (from Layer 1 registry)
            description: Discovery prose for help/search. When omitted or empty,
                derived from the implementation / data-class docstring (#194).
            metadata: Command metadata (capabilities, timeout, targeting, etc.)
            version: Semantic version (default: "1.0.0")
            bundle_id: Bundle that contributed this command (manifest name); set for bundle-sourced commands
            hot_loadable: True if this command can be dynamically loaded/unloaded (e.g. via bundle deploy)
            overwrite: Allow overwriting existing registration (used when reloading a bundle)
            scope: Optional RegistryScope; derived from the qualified name when omitted

        Raises:
            ValueError: If command_type already registered and overwrite=False
        """
        resolved_description = (description or "").strip()
        if not resolved_description:
            resolved_description = derive_command_description(
                implementation, data_class
            )

        registration = CommandRegistration(
            command_type=command_type,
            implementation_type=implementation_type,
            implementation=implementation,
            data_class=data_class,
            description=resolved_description,
            metadata=metadata or {},
            version=version,
            bundle_id=bundle_id,
            hot_loadable=hot_loadable,
        )
        resolved_scope = self._resolve_scope(command_type, bundle_id, scope)

        with self._lock:
            existing_entry = self._entries.get(command_type)
            if existing_entry is not None and not overwrite:
                logger.debug(
                    "Command type already registered, skipping",
                    command_type=command_type,
                    existing_type=existing_entry.item.implementation_type.value,
                )
                return

            if existing_entry is None:
                self._stats["registered"] += 1
                self._bump_impl_stats(implementation_type, 1)
            else:
                old_type = existing_entry.item.implementation_type
                if old_type != implementation_type:
                    self._bump_impl_stats(old_type, -1)
                    self._bump_impl_stats(implementation_type, 1)

            self._entries[command_type] = RegistryEntry(
                key=command_type,
                item=registration,
                scope=resolved_scope,
                metadata=dict(registration.metadata),
            )
            if command_type not in self._versions:
                self._versions[command_type] = {}
            self._versions[command_type][version] = registration

        logger.debug(
            "Registered command type",
            command_type=command_type,
            implementation_type=implementation_type.value,
            version=version,
            bundle_id=bundle_id,
            hot_loadable=hot_loadable,
            namespace=resolved_scope.namespace,
        )

    def register(
        self,
        key: str,
        item: CommandRegistration,
        *,
        scope: Optional[RegistryScope] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Register a command using the common registry API shape.

        Note: `metadata` exists for signature parity and is not separately stored because
        command metadata already lives inside `CommandRegistration.metadata`.
        """
        _ = metadata
        self.register_command(
            command_type=key,
            implementation=item.implementation,
            implementation_type=item.implementation_type,
            data_class=item.data_class,
            description=item.description,
            metadata=item.metadata,
            version=item.version,
            bundle_id=item.bundle_id,
            hot_loadable=item.hot_loadable,
            overwrite=True,
            scope=scope,
        )

    def get(  # type: ignore[override]
        self,
        key: str,
        version: Optional[str] = None,
    ) -> Optional[CommandRegistration]:
        """Return item payload for key or None; optional version targets a specific registration version."""
        with self._lock:
            if version:
                return self._versions.get(key, {}).get(version)
            entry = self._entries.get(key)
            return entry.item if entry else None

    def is_registered(self, command_type: str, version: Optional[str] = None) -> bool:
        """
        Check if a command type is registered.

        Args:
            command_type: Command type identifier
            version: Specific version to check (default: any version)

        Returns:
            True if registered, False otherwise
        """
        with self._lock:
            if version:
                return command_type in self._versions and version in self._versions[command_type]
            return command_type in self._entries

    def unregister(  # type: ignore[override]
        self,
        command_type: str,
        version: Optional[str] = None,
    ) -> bool:
        """
        Unregister a command type.

        Args:
            command_type: Command type identifier
            version: Specific version to unregister (default: all versions)

        Returns:
            True if unregistered, False if not found
        """
        with self._lock:
            if command_type not in self._entries:
                return False

            if version is None:
                return self._unregister_all_unlocked(command_type)

            # Unregister specific version
            versions = self._versions.get(command_type)
            if not versions or version not in versions:
                return False

            del versions[version]
            current = self._entries[command_type].item
            if current.version == version:
                if versions:
                    latest_version = max(versions.keys())
                    latest = versions[latest_version]
                    prev_entry = self._entries[command_type]
                    self._entries[command_type] = RegistryEntry(
                        key=command_type,
                        item=latest,
                        scope=prev_entry.scope,
                        metadata=dict(latest.metadata),
                    )
                else:
                    self._unregister_all_unlocked(command_type)

            logger.debug(
                "Unregistered command version",
                command_type=command_type,
                version=version,
            )
            return True

    def get_command_types(
        self,
        filter_type: Optional[CommandImplementationType] = None,
        bundle_id: Optional[str] = None,
    ) -> List[str]:
        """
        Get all registered command types with optional filtering.

        Args:
            filter_type: Filter by implementation type
            bundle_id: Filter by bundle (manifest name) for bundle-sourced commands

        Returns:
            List of command type identifiers
        """
        with self._lock:
            command_types = []
            for cmd_type, entry in self._entries.items():
                registration = entry.item
                if filter_type and registration.implementation_type != filter_type:
                    continue
                if bundle_id and registration.bundle_id != bundle_id:
                    continue
                command_types.append(cmd_type)
            return sorted(command_types)

    def get_all_registrations(self) -> Dict[str, CommandRegistration]:
        """
        Get all command registrations.

        Returns:
            Dictionary mapping command types to registrations
        """
        return self.list_items()

    def get_versions(self, command_type: str) -> List[str]:
        """
        Get all registered versions for a command type.

        Args:
            command_type: Command type identifier

        Returns:
            List of version strings, sorted in descending order
        """
        with self._lock:
            if command_type not in self._versions:
                return []
            return sorted(self._versions[command_type].keys(), reverse=True)

    def clear(self) -> None:
        """
        Clear all registered commands.

        WARNING: This should only be used in testing.
        """
        with self._lock:
            self._entries.clear()
            self._versions.clear()
            self._stats = {
                "registered": 0,
                "class_based": 0,
                "decorator_based": 0,
            }
            logger.warning("Cleared all command type registrations")

    def get_scope(self, command_type: str) -> Optional[RegistryScope]:
        """Get scope metadata for a command type."""
        entry = self.get_entry(command_type)
        return entry.scope if entry is not None else None

    def unregister_namespace(self, namespace: str) -> List[str]:
        """Remove all commands under namespace prefix and return removed keys."""
        prefix = f"{namespace}."
        with self._lock:
            removed_keys = [key for key in self._entries if key.startswith(prefix)]
            for key in removed_keys:
                self._unregister_all_unlocked(key)
        if removed_keys:
            logger.info(
                "registry_namespace_unregistered",
                registry_name=self._registry_name,
                namespace=namespace,
                removed_count=len(removed_keys),
            )
        return sorted(removed_keys)

    def get_stats(self) -> Dict[str, int]:
        """
        Get registry statistics by implementation type.

        Returns:
            Dictionary with counts by implementation type

        Note:
            Distinct from ScopedRegistry.stats(), which returns namespace totals.
        """
        with self._lock:
            return dict(self._stats)

    def instantiate_command(
        self,
        command_type: str,
        data: Union[BaseModel, Dict],
        version: Optional[str] = None,
        **kwargs: Any,
    ) -> Any:
        """
        Instantiate a command from registered type.

        Handles both class-based and decorator-based commands automatically
        based on their implementation type.

        Args:
            command_type: Command type identifier
            data: Command data (BaseModel instance or dict)
            version: Specific version to instantiate (default: latest)
            **kwargs: Additional arguments for command instantiation
                     (e.g., task_id, conversation_id, etc.)

        Returns:
            Command instance (class-based) or prepared callable (decorator-based)

        Raises:
            ValueError: If command type not registered
            TypeError: If data type doesn't match expected format
        """
        registration = self.get(command_type, version)
        if not registration:
            raise ValueError(
                f"Command type '{command_type}' not registered"
                + (f" (version {version})" if version else "")
            )

        if registration.implementation_type == CommandImplementationType.CLASS_BASED:
            return registration.implementation(data=data, **kwargs)

        if registration.implementation_type == CommandImplementationType.DECORATOR_BASED:
            return registration.implementation(data=data, **kwargs)

        raise TypeError(
            f"Unknown implementation type: {registration.implementation_type}"
        )


# Global singleton instance
command_type_registry = CommandTypeRegistry()


# Convenience functions for easier access
def register_command_type(
    command_type: str,
    implementation: Union[Type, Callable],
    implementation_type: CommandImplementationType,
    **kwargs: Any,
) -> None:
    """Convenience function to register a command type."""
    command_type_registry.register_command(
        command_type=command_type,
        implementation=implementation,
        implementation_type=implementation_type,
        **kwargs
    )


def get_command_registration(
    command_type: str,
    version: Optional[str] = None
) -> Optional[CommandRegistration]:
    """Convenience function to get a command registration."""
    return command_type_registry.get(command_type, version)


def is_command_registered(command_type: str, version: Optional[str] = None) -> bool:
    """Convenience function to check if a command is registered."""
    return command_type_registry.is_registered(command_type, version)


def get_all_command_types(
    filter_type: Optional[CommandImplementationType] = None,
    bundle_id: Optional[str] = None
) -> List[str]:
    """Convenience function to get all command types."""
    return command_type_registry.get_command_types(filter_type, bundle_id)
