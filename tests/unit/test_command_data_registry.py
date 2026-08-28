"""
Unit tests for CommandDataRegistry.

Tests the self-registration pattern and thread-safe registry operations.
"""

import pytest
import threading
from pydantic import BaseModel, Field
from typing import Optional

from motet.core.commands.command_data_registry import (
    CommandDataRegistry,
    command_data_registry,
    register_command_data,
    get_command_data_class,
    get_all_command_data_classes,
    get_command_types
)
from motet.core.commands.base_command_data import BaseCommandData


# Sample data classes (names avoid pytest collecting as test classes)
class SampleCommandData1(BaseCommandData):
    """Sample command data class 1"""
    field1: str
    field2: int = 0


class SampleCommandData2(BaseCommandData):
    """Sample command data class 2"""
    field_a: str
    field_b: Optional[str] = None


class SampleCommandData3(BaseModel):
    """Sample command data class 3 (plain Pydantic)"""
    value: str


@pytest.fixture
def registry():
    """Create a fresh registry instance for each test."""
    reg = CommandDataRegistry()
    return reg


@pytest.fixture
def clean_global_registry():
    """Clean the global registry before and after each test."""
    # Store original state
    original_registry = command_data_registry.get_all()
    original_loaders = command_data_registry._lazy_loaders.copy()
    
    # Clear for test
    command_data_registry.clear()
    
    yield command_data_registry
    
    # Restore original state
    command_data_registry.clear()
    for cmd_type, data_class in original_registry.items():
        command_data_registry.register(cmd_type, data_class)
    command_data_registry._lazy_loaders = original_loaders


class TestCommandDataRegistryBasics:
    """Test basic registry operations."""
    
    def test_register_and_get(self, registry):
        """Test registering and retrieving a data class."""
        registry.register("test_command_1", SampleCommandData1)
        
        result = registry.get("test_command_1")
        assert result == SampleCommandData1
    
    def test_register_multiple(self, registry):
        """Test registering multiple data classes."""
        registry.register("test_command_1", SampleCommandData1)
        registry.register("test_command_2", SampleCommandData2)
        
        assert registry.get("test_command_1") == SampleCommandData1
        assert registry.get("test_command_2") == SampleCommandData2
    
    def test_get_nonexistent(self, registry):
        """Test getting a non-existent command type returns None."""
        result = registry.get("nonexistent_command")
        assert result is None
    
    def test_get_all(self, registry):
        """Test getting all registered data classes."""
        registry.register("test_command_1", SampleCommandData1)
        registry.register("test_command_2", SampleCommandData2)
        
        all_classes = registry.get_all()
        assert len(all_classes) == 2
        assert all_classes["test_command_1"] == SampleCommandData1
        assert all_classes["test_command_2"] == SampleCommandData2
    
    def test_get_types(self, registry):
        """Test getting all registered command types."""
        registry.register("test_command_1", SampleCommandData1)
        registry.register("test_command_2", SampleCommandData2)
        
        types = registry.get_types()
        assert types == ["test_command_1", "test_command_2"]  # Should be sorted
    
    def test_is_registered(self, registry):
        """Test checking if a command type is registered."""
        registry.register("test_command_1", SampleCommandData1)
        
        assert registry.is_registered("test_command_1") is True
        assert registry.is_registered("nonexistent") is False


class TestCommandDataRegistryDuplicates:
    """Test duplicate registration handling."""
    
    def test_register_same_class_twice_no_error(self, registry):
        """Test registering the same class twice with same type is idempotent."""
        registry.register("test_command_1", SampleCommandData1)
        registry.register("test_command_1", SampleCommandData1)  # Should not raise
        
        assert registry.get("test_command_1") == SampleCommandData1
    
    def test_register_different_class_raises_error(self, registry):
        """Test registering different class with same type raises error."""
        registry.register("test_command_1", SampleCommandData1)
        
        with pytest.raises(ValueError, match="already registered"):
            registry.register("test_command_1", SampleCommandData2)
    
    def test_register_different_class_with_overwrite(self, registry):
        """Test registering different class with overwrite=True succeeds."""
        registry.register("test_command_1", SampleCommandData1)
        registry.register("test_command_1", SampleCommandData2, overwrite=True)
        
        assert registry.get("test_command_1") == SampleCommandData2


class TestCommandDataRegistryLazyLoading:
    """Test lazy loading functionality."""
    
    def test_register_lazy(self, registry):
        """Test lazy loading registration."""
        loaded = False
        
        def loader():
            nonlocal loaded
            loaded = True
            return SampleCommandData1
        
        registry.register_lazy("test_command_lazy", loader)
        
        # Should not be loaded yet
        assert not loaded
        assert registry.is_registered("test_command_lazy")
        
        # Get should trigger loading
        result = registry.get("test_command_lazy")
        assert loaded
        assert result == SampleCommandData1
        
        # Should be cached now
        result2 = registry.get("test_command_lazy")
        assert result2 == SampleCommandData1
    
    def test_lazy_loader_called_once(self, registry):
        """Test lazy loader is only called once."""
        call_count = 0
        
        def loader():
            nonlocal call_count
            call_count += 1
            return SampleCommandData1
        
        registry.register_lazy("test_command_lazy", loader)
        
        # First get triggers load
        registry.get("test_command_lazy")
        assert call_count == 1
        
        # Second get uses cache
        registry.get("test_command_lazy")
        assert call_count == 1
    
    def test_lazy_loader_error_returns_none(self, registry):
        """Test lazy loader error returns None and doesn't crash."""
        def failing_loader():
            raise RuntimeError("Loader failed")
        
        registry.register_lazy("test_command_lazy", failing_loader)
        
        result = registry.get("test_command_lazy")
        assert result is None


class TestCommandDataRegistryUnregister:
    """Test unregister functionality."""
    
    def test_unregister_existing(self, registry):
        """Test unregistering an existing command type."""
        registry.register("test_command_1", SampleCommandData1)
        
        result = registry.unregister("test_command_1")
        assert result is True
        assert registry.get("test_command_1") is None
    
    def test_unregister_nonexistent(self, registry):
        """Test unregistering a non-existent command type."""
        result = registry.unregister("nonexistent")
        assert result is False
    
    def test_unregister_lazy_loader(self, registry):
        """Test unregistering a lazy loader."""
        registry.register_lazy("test_command_lazy", lambda: SampleCommandData1)
        
        result = registry.unregister("test_command_lazy")
        assert result is True
        assert not registry.is_registered("test_command_lazy")


class TestCommandDataRegistryClear:
    """Test clear functionality."""
    
    def test_clear(self, registry):
        """Test clearing all registrations."""
        registry.register("test_command_1", SampleCommandData1)
        registry.register("test_command_2", SampleCommandData2)
        
        registry.clear()
        
        assert len(registry.get_all()) == 0
        assert registry.get("test_command_1") is None
        assert registry.get("test_command_2") is None


class TestCommandDataRegistryStats:
    """Test statistics functionality."""
    
    def test_get_stats(self, registry):
        """Test getting registry statistics."""
        registry.register("test_command_1", SampleCommandData1)
        registry.register("test_command_2", SampleCommandData2)
        registry.register_lazy("test_command_lazy", lambda: SampleCommandData3)
        
        stats = registry.get_stats()
        assert stats["registered"] == 2
        assert stats["lazy_loaders"] == 1


class TestCommandDataRegistryThreadSafety:
    """Test thread-safe operations."""
    
    def test_concurrent_registration(self, registry):
        """Test concurrent registration from multiple threads."""
        def register_commands(start_idx):
            for i in range(start_idx, start_idx + 10):
                registry.register(f"test_command_{i}", SampleCommandData1)
        
        threads = [
            threading.Thread(target=register_commands, args=(0,)),
            threading.Thread(target=register_commands, args=(10,)),
            threading.Thread(target=register_commands, args=(20,))
        ]
        
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        # Should have 30 registered commands
        assert len(registry.get_all()) == 30
    
    def test_concurrent_get(self, registry):
        """Test concurrent get operations."""
        registry.register("test_command_1", SampleCommandData1)
        
        results = []
        
        def get_command():
            result = registry.get("test_command_1")
            results.append(result)
        
        threads = [threading.Thread(target=get_command) for _ in range(10)]
        
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        # All results should be SampleCommandData1
        assert all(r == SampleCommandData1 for r in results)
        assert len(results) == 10


class TestConvenienceFunctions:
    """Test convenience functions."""
    
    def test_register_command_data(self, clean_global_registry):
        """Test register_command_data convenience function."""
        register_command_data("test_command_1", SampleCommandData1)
        
        result = get_command_data_class("test_command_1")
        assert result == SampleCommandData1
    
    def test_get_command_data_class(self, clean_global_registry):
        """Test get_command_data_class convenience function."""
        command_data_registry.register("test_command_1", SampleCommandData1)
        
        result = get_command_data_class("test_command_1")
        assert result == SampleCommandData1
    
    def test_get_all_command_data_classes(self, clean_global_registry):
        """Test get_all_command_data_classes convenience function."""
        command_data_registry.register("test_command_1", SampleCommandData1)
        command_data_registry.register("test_command_2", SampleCommandData2)
        
        all_classes = get_all_command_data_classes()
        assert len(all_classes) == 2
    
    def test_get_command_types(self, clean_global_registry):
        """Test get_command_types convenience function."""
        command_data_registry.register("test_command_1", SampleCommandData1)
        command_data_registry.register("test_command_2", SampleCommandData2)
        
        types = get_command_types()
        assert types == ["test_command_1", "test_command_2"]


class TestDataClassInstantiation:
    """Test that registered data classes can be instantiated."""
    
    def test_instantiate_registered_class(self, registry):
        """Test instantiating a data class retrieved from registry."""
        registry.register("test_command_1", SampleCommandData1)
        
        data_class = registry.get("test_command_1")
        instance = data_class(field1="value", field2=42)
        
        assert isinstance(instance, SampleCommandData1)
        assert instance.field1 == "value"
        assert instance.field2 == 42
    
    def test_instantiate_with_validation(self, registry):
        """Test Pydantic validation works on registered classes."""
        registry.register("test_command_1", SampleCommandData1)
        
        data_class = registry.get("test_command_1")
        
        # Should raise validation error for missing required field
        with pytest.raises(Exception):  # Pydantic ValidationError
            data_class(field2=42)  # Missing field1


class TestGlobalRegistrySingleton:
    """Test global singleton registry."""
    
    def test_global_registry_is_singleton(self):
        """Test that command_data_registry is a singleton."""
        from motet.core.commands.command_data_registry import (
            command_data_registry as registry1
        )
        from motet.core.commands.command_data_registry import (
            command_data_registry as registry2
        )
        
        assert registry1 is registry2

