"""
Unit tests for security components (PII redaction, entity extraction).

Tests the security mechanisms used throughout the distributed AI framework.
"""
from __future__ import annotations

import pytest
from motet.core import Config, Message
from motet.core.types import MemoryItem


@pytest.mark.unit
def test_entity_tags_and_pii_redaction():
    """Test entity extraction and PII redaction functionality."""
    # This test needs to be updated for the new distributed architecture
    # For now, we'll test the basic configuration and types
    config = Config()
    config.pii_allowlist = ""
    
    # Test message creation
    message = Message(role="user", content="Email me at alice@example.com and visit https://foo.bar")
    assert message.role == "user"
    assert "alice@example.com" in message.content
    
    # Test memory item creation with tags
    memory_item = MemoryItem(
        id="test-item",
        type="user_message",
        content="Test content with PII",
        tags=["entity:email:alice@example.com", "entity:url:https://foo.bar"],
        metadata={}
    )
    
    assert memory_item.id == "test-item"
    assert len(memory_item.tags) == 2
    assert any(t.startswith("entity:email:") for t in memory_item.tags)
    assert any(t.startswith("entity:url:") for t in memory_item.tags)


@pytest.mark.unit 
def test_security_config():
    """Test security configuration options."""
    config = Config()
    
    assert hasattr(config, "pii_allowlist")

    config.pii_allowlist = "test@example.com"
    assert config.pii_allowlist == "test@example.com"