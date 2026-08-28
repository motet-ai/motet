"""
Unit tests for CommandTypeRegistry (Phase 2 - ADR-0024)

Tests the unified command type registry for managing command implementations.
"""

import pytest
import threading
from typing import Dict, Any
from pydantic import BaseModel

from motet.core.commands.command_type_registry import (
    CommandTypeRegistry,
    CommandImplementationType,
    CommandRegistration,
    command_type_registry,
    register_command_type,
    get_command_registration,
    is_command_registered,
    get_all_command_types
)
from motet.core.commands.base_command_data import BaseCommandData


# ========================================
# Test Fixtures
# ========================================

class SampleCommandData(BaseCommandData):
    """Sample data class for command registry tests (name avoids pytest collecting as test class)."""
    test_field: str = "test"


class MockDistributedCommand:
    """Mock class-based command for testing."""
    def __init__(self, data=None, **kwargs):
        self.data = data
        self.kwargs = kwargs
    
    def execute(self):
        return {"status": "success", "data": self.data}


def mock_decorated_function(data: SampleCommandData, motet: Any) -> Dict[str, Any]:
    """Mock decorated function for testing."""
    return {"result": f"processed {data.test_field}"}


# ========================================
# Basic Registration Tests
# ========================================

class TestCommandTypeRegistryBasics:
    """Test basic registry operations."""
    
    def setup_method(self):
        """Clear registry before each test."""
        command_type_registry.clear()
    
    def teardown_method(self):
        """Clear registry after each test."""
        command_type_registry.clear()
    
    def test_register_class_based_command(self):
        """Test registering a class-based command."""
        command_type_registry.register_command(
            command_type="test_class_command",
            implementation=MockDistributedCommand,
            implementation_type=CommandImplementationType.CLASS_BASED,
            data_class=SampleCommandData
        )
        
        assert command_type_registry.is_registered("test_class_command")
        
        registration = command_type_registry.get("test_class_command")
        assert registration is not None
        assert registration.command_type == "test_class_command"
        assert registration.implementation_type == CommandImplementationType.CLASS_BASED
        assert registration.implementation == MockDistributedCommand
        assert registration.data_class == SampleCommandData
    
    def test_register_decorator_based_command(self):
        """Test registering a decorator-based command."""
        command_type_registry.register_command(
            command_type="test_decorator_command",
            implementation=mock_decorated_function,
            implementation_type=CommandImplementationType.DECORATOR_BASED,
            data_class=SampleCommandData
        )
        
        assert command_type_registry.is_registered("test_decorator_command")
        
        registration = command_type_registry.get("test_decorator_command")
        assert registration is not None
        assert registration.implementation_type == CommandImplementationType.DECORATOR_BASED
        assert registration.implementation == mock_decorated_function

    def test_register_command_derives_description_from_implementation_docstring(self):
        """#194 — description is first-class and auto-derived when omitted."""
        command_type_registry.register_command(
            command_type="test_desc_derive",
            implementation=mock_decorated_function,
            implementation_type=CommandImplementationType.DECORATOR_BASED,
            data_class=SampleCommandData,
        )
        registration = command_type_registry.get("test_desc_derive")
        assert registration is not None
        assert registration.description == "Mock decorated function for testing."

    def test_register_command_keeps_explicit_description(self):
        command_type_registry.register_command(
            command_type="test_desc_explicit",
            implementation=mock_decorated_function,
            implementation_type=CommandImplementationType.DECORATOR_BASED,
            data_class=SampleCommandData,
            description="Explicit discovery prose for help search.",
        )
        registration = command_type_registry.get("test_desc_explicit")
        assert registration is not None
        assert registration.description == "Explicit discovery prose for help search."
    
    def test_register_bundle_command(self):
        """Test registering a bundle-sourced command."""
        command_type_registry.register_command(
            command_type="test_bundle.my_command",
            implementation=mock_decorated_function,
            implementation_type=CommandImplementationType.DECORATOR_BASED,
            bundle_id="test_bundle",
            hot_loadable=True
        )

        registration = command_type_registry.get("test_bundle.my_command")
        assert registration is not None
        assert registration.implementation_type == CommandImplementationType.DECORATOR_BASED
        assert registration.bundle_id == "test_bundle"
        assert registration.hot_loadable is True
    
    def test_get_nonexistent_command(self):
        """Test getting a command that doesn't exist."""
        assert command_type_registry.get("nonexistent") is None
        assert not command_type_registry.is_registered("nonexistent")
    
    def test_get_all_registrations(self):
        """Test getting all registrations."""
        command_type_registry.register_command(
            "cmd1", MockDistributedCommand, CommandImplementationType.CLASS_BASED
        )
        command_type_registry.register_command(
            "cmd2", mock_decorated_function, CommandImplementationType.DECORATOR_BASED
        )
        
        registrations = command_type_registry.get_all_registrations()
        assert len(registrations) == 2
        assert "cmd1" in registrations
        assert "cmd2" in registrations
    
    def test_get_command_types_all(self):
        """Test getting all command types."""
        command_type_registry.register_command(
            "cmd1", MockDistributedCommand, CommandImplementationType.CLASS_BASED
        )
        command_type_registry.register_command(
            "cmd2", mock_decorated_function, CommandImplementationType.DECORATOR_BASED
        )
        
        types = command_type_registry.get_command_types()
        assert len(types) == 2
        assert "cmd1" in types
        assert "cmd2" in types
        assert types == sorted(types)  # Should be sorted


# ========================================
# Filtering Tests
# ========================================

class TestCommandTypeFiltering:
    """Test filtering command types."""
    
    def setup_method(self):
        """Clear registry and add test commands."""
        command_type_registry.clear()
        
        # Add class-based commands
        command_type_registry.register_command(
            "class_cmd1", MockDistributedCommand, CommandImplementationType.CLASS_BASED
        )
        command_type_registry.register_command(
            "class_cmd2", MockDistributedCommand, CommandImplementationType.CLASS_BASED
        )
        
        # Add decorator-based commands
        command_type_registry.register_command(
            "decorator_cmd1", mock_decorated_function, CommandImplementationType.DECORATOR_BASED
        )
        
        # Add bundle-sourced commands (DECORATOR_BASED + bundle_id)
        command_type_registry.register_command(
            "bundle_a.cmd1", mock_decorated_function, CommandImplementationType.DECORATOR_BASED,
            bundle_id="plugin_a", hot_loadable=True
        )
        command_type_registry.register_command(
            "bundle_b.cmd2", mock_decorated_function, CommandImplementationType.DECORATOR_BASED,
            bundle_id="plugin_b", hot_loadable=True
        )
    
    def teardown_method(self):
        """Clear registry after each test."""
        command_type_registry.clear()
    
    def test_filter_by_class_based(self):
        """Test filtering for class-based commands."""
        types = command_type_registry.get_command_types(
            filter_type=CommandImplementationType.CLASS_BASED
        )
        assert len(types) == 2
        assert "class_cmd1" in types
        assert "class_cmd2" in types
    
    def test_filter_by_decorator_based(self):
        """Test filtering for decorator-based commands."""
        types = command_type_registry.get_command_types(
            filter_type=CommandImplementationType.DECORATOR_BASED
        )
        assert len(types) == 3  # decorator_cmd1 + bundle_a.cmd1 + bundle_b.cmd2
        assert "decorator_cmd1" in types
        assert "bundle_a.cmd1" in types
        assert "bundle_b.cmd2" in types

    def test_filter_by_bundle_id(self):
        """Test filtering by bundle ID."""
        types = command_type_registry.get_command_types(bundle_id="plugin_a")
        assert len(types) == 1
        assert "bundle_a.cmd1" in types

        types = command_type_registry.get_command_types(bundle_id="plugin_b")
        assert len(types) == 1
        assert "bundle_b.cmd2" in types

    def test_filter_combined(self):
        """Test combined filtering (type + bundle_id)."""
        types = command_type_registry.get_command_types(
            filter_type=CommandImplementationType.DECORATOR_BASED,
            bundle_id="plugin_a"
        )
        assert len(types) == 1
        assert "bundle_a.cmd1" in types


# ========================================
# Version Management Tests
# ========================================

class TestVersionManagement:
    """Test command version management."""
    
    def setup_method(self):
        """Clear registry before each test."""
        command_type_registry.clear()
    
    def teardown_method(self):
        """Clear registry after each test."""
        command_type_registry.clear()
    
    def test_register_multiple_versions(self):
        """Test registering multiple versions of same command."""
        # Register v1.0.0
        command_type_registry.register_command(
            "versioned_cmd", MockDistributedCommand,
            CommandImplementationType.CLASS_BASED,
            version="1.0.0",
            overwrite=True
        )
        
        # Register v2.0.0
        command_type_registry.register_command(
            "versioned_cmd", MockDistributedCommand,
            CommandImplementationType.CLASS_BASED,
            version="2.0.0",
            overwrite=True
        )
        
        # Get latest (should be v2.0.0)
        registration = command_type_registry.get("versioned_cmd")
        assert registration.version == "2.0.0"
        
        # Get specific versions
        v1 = command_type_registry.get("versioned_cmd", version="1.0.0")
        assert v1.version == "1.0.0"
        
        v2 = command_type_registry.get("versioned_cmd", version="2.0.0")
        assert v2.version == "2.0.0"
    
    def test_get_all_versions(self):
        """Test getting all versions of a command."""
        command_type_registry.register_command(
            "versioned_cmd", MockDistributedCommand,
            CommandImplementationType.CLASS_BASED,
            version="1.0.0",
            overwrite=True
        )
        command_type_registry.register_command(
            "versioned_cmd", MockDistributedCommand,
            CommandImplementationType.CLASS_BASED,
            version="2.0.0",
            overwrite=True
        )
        command_type_registry.register_command(
            "versioned_cmd", MockDistributedCommand,
            CommandImplementationType.CLASS_BASED,
            version="1.5.0",
            overwrite=True
        )
        
        versions = command_type_registry.get_versions("versioned_cmd")
        assert len(versions) == 3
        # Should be sorted in descending order
        assert versions == ["2.0.0", "1.5.0", "1.0.0"]
    
    def test_is_registered_with_version(self):
        """Test checking registration for specific version."""
        command_type_registry.register_command(
            "versioned_cmd", MockDistributedCommand,
            CommandImplementationType.CLASS_BASED,
            version="1.0.0"
        )
        
        assert command_type_registry.is_registered("versioned_cmd")
        assert command_type_registry.is_registered("versioned_cmd", version="1.0.0")
        assert not command_type_registry.is_registered("versioned_cmd", version="2.0.0")


# ========================================
# Unregistration Tests
# ========================================

class TestUnregistration:
    """Test command unregistration."""
    
    def setup_method(self):
        """Clear registry before each test."""
        command_type_registry.clear()
    
    def teardown_method(self):
        """Clear registry after each test."""
        command_type_registry.clear()
    
    def test_unregister_command(self):
        """Test unregistering a command."""
        command_type_registry.register_command(
            "test_cmd", MockDistributedCommand, CommandImplementationType.CLASS_BASED
        )
        assert command_type_registry.is_registered("test_cmd")
        
        result = command_type_registry.unregister("test_cmd")
        assert result is True
        assert not command_type_registry.is_registered("test_cmd")
    
    def test_unregister_nonexistent(self):
        """Test unregistering a command that doesn't exist."""
        result = command_type_registry.unregister("nonexistent")
        assert result is False
    
    def test_unregister_specific_version(self):
        """Test unregistering a specific version."""
        command_type_registry.register_command(
            "versioned_cmd", MockDistributedCommand,
            CommandImplementationType.CLASS_BASED,
            version="1.0.0",
            overwrite=True
        )
        command_type_registry.register_command(
            "versioned_cmd", MockDistributedCommand,
            CommandImplementationType.CLASS_BASED,
            version="2.0.0",
            overwrite=True
        )
        
        # Unregister v1.0.0
        result = command_type_registry.unregister("versioned_cmd", version="1.0.0")
        assert result is True
        
        # v2.0.0 should still be registered
        assert command_type_registry.is_registered("versioned_cmd")
        assert not command_type_registry.is_registered("versioned_cmd", version="1.0.0")
        assert command_type_registry.is_registered("versioned_cmd", version="2.0.0")
    
    def test_unregister_current_version_updates_latest(self):
        """Test that unregistering current version updates to next highest."""
        command_type_registry.register_command(
            "versioned_cmd", MockDistributedCommand,
            CommandImplementationType.CLASS_BASED,
            version="1.0.0",
            overwrite=True
        )
        command_type_registry.register_command(
            "versioned_cmd", MockDistributedCommand,
            CommandImplementationType.CLASS_BASED,
            version="2.0.0",
            overwrite=True
        )
        
        # Latest should be v2.0.0
        assert command_type_registry.get("versioned_cmd").version == "2.0.0"
        
        # Unregister v2.0.0
        command_type_registry.unregister("versioned_cmd", version="2.0.0")
        
        # Latest should now be v1.0.0
        registration = command_type_registry.get("versioned_cmd")
        assert registration is not None
        assert registration.version == "1.0.0"

    def test_unregister_namespace_clears_versions_and_stats(self):
        """ScopedRegistry namespace unload clears versions and implementation stats (#61)."""
        command_type_registry.register_command(
            "bundle_ns.cmd_a",
            MockDistributedCommand,
            CommandImplementationType.CLASS_BASED,
            version="1.0.0",
            bundle_id="bundle_ns",
            hot_loadable=True,
            overwrite=True,
        )
        command_type_registry.register_command(
            "bundle_ns.cmd_a",
            MockDistributedCommand,
            CommandImplementationType.CLASS_BASED,
            version="2.0.0",
            bundle_id="bundle_ns",
            hot_loadable=True,
            overwrite=True,
        )
        command_type_registry.register_command(
            "bundle_ns.cmd_b",
            lambda **kwargs: None,
            CommandImplementationType.DECORATOR_BASED,
            bundle_id="bundle_ns",
            hot_loadable=True,
            overwrite=True,
        )
        command_type_registry.register_command(
            "other.keep",
            MockDistributedCommand,
            CommandImplementationType.CLASS_BASED,
            overwrite=True,
        )

        removed = command_type_registry.unregister_namespace("bundle_ns")
        assert removed == ["bundle_ns.cmd_a", "bundle_ns.cmd_b"]
        assert not command_type_registry.is_registered("bundle_ns.cmd_a")
        assert command_type_registry.get_versions("bundle_ns.cmd_a") == []
        assert command_type_registry.is_registered("other.keep")
        stats = command_type_registry.get_stats()
        assert stats["registered"] == 1
        assert stats["class_based"] == 1
        assert stats["decorator_based"] == 0


# ========================================
# Metadata Tests
# ========================================

class TestMetadata:
    """Test command metadata handling."""
    
    def setup_method(self):
        """Clear registry before each test."""
        command_type_registry.clear()
    
    def teardown_method(self):
        """Clear registry after each test."""
        command_type_registry.clear()
    
    def test_register_with_metadata(self):
        """Test registering command with metadata."""
        metadata = {
            "timeout_seconds": 120,
            "priority": 5,
            "required_capabilities": ["model_inference", "tool_execution"],
            "streaming_enabled": True
        }
        
        command_type_registry.register_command(
            "test_cmd", MockDistributedCommand,
            CommandImplementationType.CLASS_BASED,
            metadata=metadata
        )
        
        registration = command_type_registry.get("test_cmd")
        assert registration.metadata == metadata
    
    def test_empty_metadata(self):
        """Test registering without metadata."""
        command_type_registry.register_command(
            "test_cmd", MockDistributedCommand,
            CommandImplementationType.CLASS_BASED
        )
        
        registration = command_type_registry.get("test_cmd")
        assert registration.metadata == {}


# ========================================
# Statistics Tests
# ========================================

class TestStatistics:
    """Test registry statistics."""
    
    def setup_method(self):
        """Clear registry before each test."""
        command_type_registry.clear()
    
    def teardown_method(self):
        """Clear registry after each test."""
        command_type_registry.clear()
    
    def test_stats_empty_registry(self):
        """Test stats for empty registry."""
        stats = command_type_registry.get_stats()
        assert stats['registered'] == 0
        assert stats['class_based'] == 0
        assert stats['decorator_based'] == 0
    
    def test_stats_with_commands(self):
        """Test stats with registered commands."""
        command_type_registry.register_command(
            "class_cmd", MockDistributedCommand, CommandImplementationType.CLASS_BASED
        )
        command_type_registry.register_command(
            "decorator_cmd", mock_decorated_function, CommandImplementationType.DECORATOR_BASED
        )
        command_type_registry.register_command(
            "bundle.cmd", mock_decorated_function, CommandImplementationType.DECORATOR_BASED,
            bundle_id="bundle", hot_loadable=True
        )

        stats = command_type_registry.get_stats()
        assert stats['registered'] == 3
        assert stats['class_based'] == 1
        assert stats['decorator_based'] == 2  # decorator_cmd + bundle.cmd


# ========================================
# Thread Safety Tests
# ========================================

class TestThreadSafety:
    """Test thread-safe concurrent access."""
    
    def setup_method(self):
        """Clear registry before each test."""
        command_type_registry.clear()
    
    def teardown_method(self):
        """Clear registry after each test."""
        command_type_registry.clear()
    
    def test_concurrent_registration(self):
        """Test concurrent command registration."""
        def register_command(cmd_id):
            command_type_registry.register_command(
                f"cmd_{cmd_id}",
                MockDistributedCommand,
                CommandImplementationType.CLASS_BASED
            )
        
        threads = []
        for i in range(10):
            t = threading.Thread(target=register_command, args=(i,))
            threads.append(t)
            t.start()
        
        for t in threads:
            t.join()
        
        # All commands should be registered
        types = command_type_registry.get_command_types()
        assert len(types) == 10
        for i in range(10):
            assert f"cmd_{i}" in types
    
    def test_concurrent_read_write(self):
        """Test concurrent reads and writes."""
        def register_commands():
            for i in range(5):
                command_type_registry.register_command(
                    f"cmd_{i}",
                    MockDistributedCommand,
                    CommandImplementationType.CLASS_BASED
                )
        
        def read_commands():
            for i in range(5):
                command_type_registry.get(f"cmd_{i}")
        
        threads = []
        # Start write thread
        t1 = threading.Thread(target=register_commands)
        threads.append(t1)
        t1.start()
        
        # Start multiple read threads
        for _ in range(3):
            t = threading.Thread(target=read_commands)
            threads.append(t)
            t.start()
        
        for t in threads:
            t.join()
        
        # No assertion - just ensuring no deadlock or race conditions


# ========================================
# Convenience Function Tests
# ========================================

class TestConvenienceFunctions:
    """Test module-level convenience functions."""
    
    def setup_method(self):
        """Clear registry before each test."""
        command_type_registry.clear()
    
    def teardown_method(self):
        """Clear registry after each test."""
        command_type_registry.clear()
    
    def test_register_command_type_function(self):
        """Test register_command_type convenience function."""
        register_command_type(
            "test_cmd",
            MockDistributedCommand,
            CommandImplementationType.CLASS_BASED
        )
        assert is_command_registered("test_cmd")
    
    def test_get_command_registration_function(self):
        """Test get_command_registration convenience function."""
        register_command_type(
            "test_cmd",
            MockDistributedCommand,
            CommandImplementationType.CLASS_BASED
        )
        registration = get_command_registration("test_cmd")
        assert registration is not None
        assert registration.command_type == "test_cmd"
    
    def test_is_command_registered_function(self):
        """Test is_command_registered convenience function."""
        assert not is_command_registered("test_cmd")
        
        register_command_type(
            "test_cmd",
            MockDistributedCommand,
            CommandImplementationType.CLASS_BASED
        )
        assert is_command_registered("test_cmd")
    
    def test_get_all_command_types_function(self):
        """Test get_all_command_types convenience function."""
        register_command_type(
            "cmd1",
            MockDistributedCommand,
            CommandImplementationType.CLASS_BASED
        )
        register_command_type(
            "cmd2",
            mock_decorated_function,
            CommandImplementationType.DECORATOR_BASED
        )
        
        types = get_all_command_types()
        assert len(types) == 2
        assert "cmd1" in types
        assert "cmd2" in types


# ========================================
# Singleton Tests
# ========================================

class TestSingleton:
    """Test singleton pattern."""
    
    def test_global_registry_is_singleton(self):
        """Test that command_type_registry is a singleton."""
        from motet.core.commands.command_type_registry import command_type_registry as registry1
        from motet.core.commands.command_type_registry import command_type_registry as registry2
        
        assert registry1 is registry2
        assert id(registry1) == id(registry2)
    
    def test_new_instance_returns_same_object(self):
        """Test that creating new CommandTypeRegistry returns singleton."""
        instance1 = CommandTypeRegistry()
        instance2 = CommandTypeRegistry()
        
        assert instance1 is instance2
        assert id(instance1) == id(instance2)

