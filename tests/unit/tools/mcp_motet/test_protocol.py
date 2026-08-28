# tests/unit/tools/mcp_motet/test_protocol.py
"""
Test suite for Motet MCP protocol definitions (ADR-0058).

Validates message formats, stream naming conventions, and instance key generation
as specified in ADR-0058.
"""

import pytest
import json
from typing import Dict, Any

from motet.core.tools.mcp_motet.protocol import (
    StreamType, Visibility, LifecycleDuration, StateModel, CredentialScope,
    MCPStreamMessage, MCPRequestMessage, 
    MCPResponseMessage, MCPLogMessage, MCPControlMessage, MCPEventMessage,
    generate_stream_name, parse_stream_name, generate_instance_key,
    logical_mcp_bus_name, logical_mcp_stream_name, manager_id_from_stream_name,
    mcp_io_stream_scan_patterns,
    parse_instance_key, resolve_visibility_and_lifecycle, validate_instance_spec
)


class TestStreamTypes:
    """Test stream type enumeration."""
    
    def test_stream_type_values(self):
        """Validate stream type enum values match ADR specification."""
        assert StreamType.REQUESTS == "requests"
        assert StreamType.RESPONSES == "responses"
        assert StreamType.NOTIFICATIONS == "notifications"
        assert StreamType.LOGS == "logs"
        assert StreamType.CONTROL == "control"
        assert StreamType.EVENTS == "events"


class TestVisibilityAndLifecycle:
    """Test visibility and lifecycle duration enumerations (ADR-0058)."""
    
    def test_visibility_values(self):
        """Validate visibility enum values."""
        assert Visibility.MOTET == "motet"
        assert Visibility.GLOBAL == "global"
        assert Visibility.TENANT == "tenant"
        assert Visibility.USER == "user"
    
    def test_lifecycle_duration_values(self):
        """Validate lifecycle duration enum values."""
        assert LifecycleDuration.PERMANENT == "permanent"
        assert LifecycleDuration.SESSION == "session"
        assert LifecycleDuration.CONVERSATION == "conversation"
        assert LifecycleDuration.TASK == "task"
        assert LifecycleDuration.IDLE_TIMEOUT == "idle_timeout"


class TestMessageFormats:
    """Test message format validation and serialization (ADR-0058)."""
    
    def test_mcp_request_message_creation(self):
        """Test MCPRequestMessage creation and validation."""
        instance_key = "playwright:default:production:user-123:conversation:conv-abc123"
        request_msg = MCPRequestMessage(
            service_id="playwright",
            instance_key=instance_key,
            worker_id="worker-abc",
            timeout_ms=30000,
            jsonrpc_request={
                "jsonrpc": "2.0",
                "id": "req-123",
                "method": "tools/call",
                "params": {
                    "name": "screenshot",
                    "arguments": {"url": "https://example.com"}
                }
            }
        )
        
        assert request_msg.service_id == "playwright"
        assert request_msg.instance_key == instance_key
        assert request_msg.worker_id == "worker-abc"
        assert request_msg.timeout_ms == 30000
        assert request_msg.stream_type == StreamType.REQUESTS
        assert request_msg.jsonrpc_request["method"] == "tools/call"
        assert request_msg.id is not None
        assert request_msg.timestamp > 0
    
    def test_mcp_response_message_creation(self):
        """Test MCPResponseMessage creation and validation."""
        instance_key = "playwright:default:production:user-123:conversation:conv-abc123"
        response_msg = MCPResponseMessage(
            service_id="playwright",
            instance_key=instance_key,
            request_id="req-123",
            processing_time_ms=1250,
            jsonrpc_response={
                "jsonrpc": "2.0",
                "id": "req-123",
                "result": {
                    "content": [{"type": "text", "text": "Screenshot saved"}]
                }
            }
        )
        
        assert response_msg.service_id == "playwright"
        assert response_msg.request_id == "req-123"
        assert response_msg.processing_time_ms == 1250
        assert response_msg.stream_type == StreamType.RESPONSES
        assert "result" in response_msg.jsonrpc_response
    
    def test_mcp_log_message_creation(self):
        """Test MCPLogMessage creation and validation."""
        instance_key = "playwright:default:production:user-123:conversation:conv-abc123"
        log_msg = MCPLogMessage(
            service_id="playwright",
            instance_key=instance_key,
            request_id="req-123",
            level="info",
            message="Processing screenshot request",
            raw_stderr="Processing screenshot request..."
        )
        
        assert log_msg.service_id == "playwright"
        assert log_msg.level == "info"
        assert log_msg.message == "Processing screenshot request"
        assert log_msg.raw_stderr == "Processing screenshot request..."
        assert log_msg.stream_type == StreamType.LOGS
    
    def test_mcp_control_message_creation(self):
        """Test MCPControlMessage creation and validation."""
        instance_key = "playwright:default:production:user-123:conversation:conv-abc123"
        control_msg = MCPControlMessage(
            service_id="playwright",
            instance_key=instance_key,
            command="restart",
            params={"reason": "health_check_failed"}
        )
        
        assert control_msg.service_id == "playwright"
        assert control_msg.command == "restart"
        assert control_msg.params["reason"] == "health_check_failed"
        assert control_msg.stream_type == StreamType.CONTROL
    
    def test_mcp_event_message_creation(self):
        """Test MCPEventMessage creation and validation."""
        instance_key = "playwright:default:production:user-123:conversation:conv-abc123"
        event_msg = MCPEventMessage(
            service_id="playwright",
            instance_key=instance_key,
            event_type="started",
            event_data={"process_pid": 12345}
        )
        
        assert event_msg.service_id == "playwright"
        assert event_msg.event_type == "started"
        assert event_msg.event_data["process_pid"] == 12345
        assert event_msg.stream_type == StreamType.EVENTS
    
    def test_message_serialization(self):
        """Test message JSON serialization and deserialization."""
        instance_key = "playwright:default:production:user-123:conversation:conv-abc123"
        request_msg = MCPRequestMessage(
            service_id="playwright",
            instance_key=instance_key,
            jsonrpc_request={"jsonrpc": "2.0", "id": "req-123", "method": "tools/call"}
        )
        
        # Serialize to JSON
        json_str = request_msg.model_dump_json()
        assert isinstance(json_str, str)
        
        # Parse JSON
        json_data = json.loads(json_str)
        assert json_data["service_id"] == "playwright"
        assert json_data["instance_key"] == instance_key
        assert json_data["stream_type"] == "requests"
        
        # Deserialize back to object
        reconstructed = MCPRequestMessage(**json_data)
        assert reconstructed.service_id == request_msg.service_id
        assert reconstructed.instance_key == request_msg.instance_key


class TestInstanceKeyGeneration:
    """Test instance key generation (ADR-0058)."""
    
    def test_global_visibility_instance_key(self):
        """Test instance key generation for GLOBAL visibility."""
        key = generate_instance_key(
            service_id="weather",
            visibility=Visibility.GLOBAL,
            lifecycle_duration=LifecycleDuration.PERMANENT,
            motet_id="default"
        )
        assert key == "weather:global"
    
    def test_tenant_visibility_instance_key(self):
        """Test instance key generation for TENANT visibility."""
        key = generate_instance_key(
            service_id="weather",
            visibility=Visibility.TENANT,
            lifecycle_duration=LifecycleDuration.PERMANENT,
            motet_id="default",
            tenant_id="acme-corp"
        )
        assert key == "weather:acme-corp"
    
    def test_motet_visibility_instance_key(self):
        """Test instance key generation for MOTET visibility."""
        key = generate_instance_key(
            service_id="weather",
            visibility=Visibility.MOTET,
            lifecycle_duration=LifecycleDuration.PERMANENT,
            motet_id="production",
            tenant_id="acme-corp"
        )
        assert key == "weather:acme-corp:production"
    
    def test_user_visibility_instance_key(self):
        """Test instance key generation for USER visibility."""
        key = generate_instance_key(
            service_id="google_workspace",
            visibility=Visibility.USER,
            lifecycle_duration=LifecycleDuration.PERMANENT,
            motet_id="production",
            tenant_id="acme-corp",
            principal_id="user-123"
        )
        assert key == "google_workspace:acme-corp:production:user-123"
    
    def test_conversation_lifecycle_instance_key(self):
        """Test instance key generation with CONVERSATION lifecycle."""
        key = generate_instance_key(
            service_id="playwright",
            visibility=Visibility.USER,
            lifecycle_duration=LifecycleDuration.CONVERSATION,
            motet_id="production",
            tenant_id="acme-corp",
            principal_id="user-123",
            conversation_id="conv-abc123"
        )
        assert key == "playwright:acme-corp:production:user-123:conversation:conv-abc123"
    
    def test_task_lifecycle_instance_key(self):
        """Test instance key generation with TASK lifecycle."""
        key = generate_instance_key(
            service_id="playwright",
            visibility=Visibility.USER,
            lifecycle_duration=LifecycleDuration.TASK,
            motet_id="production",
            tenant_id="acme-corp",
            principal_id="user-123",
            task_id="task-xyz789"
        )
        assert key == "playwright:acme-corp:production:user-123:task:task-xyz789"
    
    def test_session_lifecycle_instance_key(self):
        """Test instance key generation with SESSION lifecycle."""
        key = generate_instance_key(
            service_id="playwright",
            visibility=Visibility.USER,
            lifecycle_duration=LifecycleDuration.SESSION,
            motet_id="production",
            tenant_id="acme-corp",
            principal_id="user-123",
            session_id="session-456"
        )
        assert key == "playwright:acme-corp:production:user-123:session:session-456"
    
    def test_instance_key_validation_errors(self):
        """Test instance key generation raises errors for invalid inputs."""
        # Missing tenant_id for MOTET visibility
        with pytest.raises(ValueError, match="requires tenant_id"):
            generate_instance_key(
                service_id="weather",
                visibility=Visibility.MOTET,
                lifecycle_duration=LifecycleDuration.PERMANENT,
                motet_id="production"
            )
        
        # Missing tenant_id and principal_id for USER visibility
        with pytest.raises(ValueError, match="requires tenant_id and principal_id"):
            generate_instance_key(
                service_id="google_workspace",
                visibility=Visibility.USER,
                lifecycle_duration=LifecycleDuration.PERMANENT,
                motet_id="production"
            )
        
        # Missing conversation_id for CONVERSATION lifecycle
        with pytest.raises(ValueError, match="requires conversation_id"):
            generate_instance_key(
                service_id="playwright",
                visibility=Visibility.USER,
                lifecycle_duration=LifecycleDuration.CONVERSATION,
                motet_id="production",
                tenant_id="acme-corp",
                principal_id="user-123"
            )


class TestInstanceKeyParsing:
    """Test instance key parsing (ADR-0058). API: parse_instance_key(service_id, visibility, instance_key)."""
    
    def test_parse_global_instance_key(self):
        """Test parsing GLOBAL visibility instance key."""
        parsed = parse_instance_key("weather", Visibility.GLOBAL, "weather:global")
        assert parsed["tenant_id"] is None
        assert parsed["motet_id"] is None
        assert parsed["principal_id"] is None
    
    def test_parse_tenant_instance_key(self):
        """Test parsing TENANT visibility instance key."""
        parsed = parse_instance_key("weather", Visibility.TENANT, "weather:acme-corp")
        assert parsed["tenant_id"] == "acme-corp"
        assert parsed["motet_id"] is None
        assert parsed["principal_id"] is None
    
    def test_parse_motet_instance_key(self):
        """Test parsing MOTET visibility instance key."""
        parsed = parse_instance_key("weather", Visibility.MOTET, "weather:acme-corp:production")
        assert parsed["tenant_id"] == "acme-corp"
        assert parsed["motet_id"] == "production"
        assert parsed["principal_id"] is None
    
    def test_parse_user_instance_key(self):
        """Test parsing USER visibility instance key."""
        parsed = parse_instance_key("google_workspace", Visibility.USER, "google_workspace:acme-corp:production:user-123")
        assert parsed["tenant_id"] == "acme-corp"
        assert parsed["motet_id"] == "production"
        assert parsed["principal_id"] == "user-123"
    
    def test_parse_conversation_lifecycle_key(self):
        """Test parsing instance key with CONVERSATION lifecycle suffix."""
        parsed = parse_instance_key("playwright", Visibility.USER, "playwright:acme-corp:production:user-123:conversation:conv-abc123")
        assert parsed["tenant_id"] == "acme-corp"
        assert parsed["motet_id"] == "production"
        assert parsed["principal_id"] == "user-123"
        assert parsed["conversation_id"] == "conv-abc123"


class TestStreamNaming:
    """Test stream naming conventions (ADR-0058 / issue #235)."""
    
    def test_generate_stream_name_global(self):
        """GLOBAL stays unprefixed (no customer tenant)."""
        instance_key = "weather:global"
        stream_name = generate_stream_name(
            "weather", Visibility.GLOBAL, instance_key, StreamType.REQUESTS
        )
        assert stream_name == "mcp:mcp-weather-global-global-requests"

    def test_generate_stream_name_global_with_manager(self):
        """GLOBAL + manager is bus-prefixed only."""
        stream_name = generate_stream_name(
            "weather",
            Visibility.GLOBAL,
            "weather:global",
            StreamType.REQUESTS,
            manager_id="mcp-local-default",
        )
        assert stream_name == "mcp:mcp-local-default:mcp-weather-global-global-requests"
    
    def test_generate_stream_name_tenant(self):
        """TENANT streams get a leading tenant segment."""
        instance_key = "weather:acme-corp"
        stream_name = generate_stream_name(
            "weather", Visibility.TENANT, instance_key, StreamType.RESPONSES
        )
        assert stream_name == "acme-corp:mcp:mcp-weather-tenant-acme-corp-responses"
    
    def test_generate_stream_name_motet(self):
        """MOTET streams are tenant-prefixed."""
        instance_key = "weather:acme-corp:production"
        stream_name = generate_stream_name(
            "weather", Visibility.MOTET, instance_key, StreamType.REQUESTS
        )
        assert stream_name == "acme-corp:mcp:mcp-weather-motet-acme-corp:production-requests"
    
    def test_generate_stream_name_user(self):
        """USER streams are tenant-prefixed."""
        instance_key = "google_workspace:acme-corp:production:user-123"
        stream_name = generate_stream_name(
            "google_workspace", Visibility.USER, instance_key, StreamType.REQUESTS
        )
        assert stream_name == (
            "acme-corp:mcp:mcp-google_workspace-user-acme-corp:production:user-123-requests"
        )
    
    def test_generate_stream_name_with_lifecycle(self):
        """Lifecycle suffix stays in the logical body."""
        instance_key = "playwright:acme-corp:production:user-123:conversation:conv-abc123"
        stream_name = generate_stream_name(
            "playwright", Visibility.USER, instance_key, StreamType.RESPONSES
        )
        assert stream_name == (
            "acme-corp:mcp:mcp-playwright-user-acme-corp:production:user-123"
            ":conversation:conv-abc123-responses"
        )
    
    def test_generate_stream_name_with_manager_id(self):
        """Physical key is {tid}:mcp:{manager}:mcp-… — family then manager."""
        instance_key = "weather:acme-corp:production"
        stream_name = generate_stream_name(
            "weather",
            Visibility.MOTET,
            instance_key,
            StreamType.REQUESTS,
            manager_id="mcp-local-default",
        )
        assert stream_name == (
            "acme-corp:mcp:mcp-local-default:mcp-weather-motet-acme-corp:production-requests"
        )
        assert logical_mcp_stream_name(
            "weather", Visibility.MOTET, instance_key, StreamType.REQUESTS
        ) == "mcp-weather-motet-acme-corp:production-requests"
    
    def test_parse_stream_name_simple(self):
        """Test parsing simple stream names."""
        parsed = parse_stream_name("mcp-weather-global-global-requests")
        
        assert parsed["service_id"] == "weather"
        assert parsed["visibility"] == Visibility.GLOBAL
        assert parsed["stream_type"] == StreamType.REQUESTS
        assert "instance_key" in parsed
        assert "tenant_id" not in parsed
        assert "manager_id" not in parsed
    
    def test_parse_stream_name_tenant_and_manager(self):
        """Parse {tid}:mcp:{manager}:mcp-… without treating manager as tenant."""
        parsed = parse_stream_name(
            "acme-corp:mcp:mcp-local-default:mcp-playwright-user-"
            "acme-corp:production:user-123-responses"
        )
        
        assert parsed["service_id"] == "playwright"
        assert parsed["visibility"] == Visibility.USER
        assert parsed["stream_type"] == StreamType.RESPONSES
        assert parsed["tenant_id"] == "acme-corp"
        assert parsed["manager_id"] == "mcp-local-default"
        assert parsed["instance_key"] == "playwright:acme-corp:production:user-123"
        assert manager_id_from_stream_name(
            "acme-corp:mcp:mcp-local-default:mcp-playwright-user-"
            "acme-corp:production:user-123-responses"
        ) == "mcp-local-default"

    def test_parse_stream_name_does_not_treat_manager_id_as_logical_body(self):
        """mcp-local-default starts with mcp- but is the manager, not the stream body."""
        parsed = parse_stream_name(
            "mcp:mcp-local-default:mcp-weather-global-global-requests"
        )
        assert parsed["manager_id"] == "mcp-local-default"
        assert parsed["service_id"] == "weather"
        assert parsed["visibility"] == Visibility.GLOBAL
        assert "tenant_id" not in parsed

    def test_logical_mcp_bus_name_strips_tenant_keeps_manager(self):
        physical = (
            "acme-corp:mcp:mcp-local-default:mcp-weather-motet-acme-corp:production-requests"
        )
        assert logical_mcp_bus_name(physical) == (
            "mcp-local-default:mcp-weather-motet-acme-corp:production-requests"
        )

    def test_mcp_io_stream_scan_patterns_include_tenant_and_global(self):
        patterns = mcp_io_stream_scan_patterns(
            "mcp-local-default", stream_type="requests"
        )
        assert "mcp:mcp-local-default:mcp-*-requests" in patterns
        assert "*:mcp:mcp-local-default:mcp-*-requests" in patterns
    
    def test_parse_stream_name_invalid_format(self):
        """Test parsing invalid stream names raises ValueError."""
        with pytest.raises(ValueError, match="Invalid stream name format"):
            parse_stream_name("invalid-stream-name")
        
        with pytest.raises(ValueError, match="Invalid stream name format"):
            parse_stream_name("not-mcp-playwright-shared-default-requests")

        # Legacy ADR-0020/0057 stream format is no longer supported.
        with pytest.raises(ValueError, match="Invalid stream name format"):
            parse_stream_name("mcp-playwright-shared-default-requests")

        # Pre-#235 physical keys ({manager}:mcp-…) are not accepted.
        with pytest.raises(ValueError, match="Invalid stream name format"):
            parse_stream_name(
                "mcp-local-default:mcp-weather-motet-acme-corp:production-requests"
            )

    def test_tenant_acl_glob_matches_only_same_tenant_stream(self):
        from fnmatch import fnmatch

        acme = generate_stream_name(
            "weather",
            Visibility.MOTET,
            "weather:acme-corp:production",
            StreamType.REQUESTS,
            manager_id="mcp-local-default",
        )
        other = generate_stream_name(
            "weather",
            Visibility.MOTET,
            "weather:other:production",
            StreamType.REQUESTS,
            manager_id="mcp-local-default",
        )
        assert fnmatch(acme, "acme-corp:*")
        assert not fnmatch(other, "acme-corp:*")

    def test_aad_stream_key_is_bus_name_not_physical_tenant_key(self):
        from motet.core.security.aad_helpers import compute_mcp_stream_aad

        physical = (
            "acme-corp:mcp:mcp-local-default:mcp-weather-motet-acme-corp:production-requests"
        )
        bus = logical_mcp_bus_name(physical)
        assert bus == "mcp-local-default:mcp-weather-motet-acme-corp:production-requests"
        kwargs = {
            "message_type": "requests",
            "request_id": "req-1",
            "tenant_id": "acme-corp",
            "motet_id": "production",
            "service_id": "weather",
        }
        assert compute_mcp_stream_aad(stream_key=physical, **kwargs) != (
            compute_mcp_stream_aad(stream_key=bus, **kwargs)
        )


class TestVisibilityAndLifecycleResolution:
    """Test visibility and lifecycle resolution heuristics."""
    
    def test_resolve_user_visibility(self):
        """Test resolution to USER visibility when principal_id present."""
        visibility, lifecycle = resolve_visibility_and_lifecycle(
            tenant_id="acme-corp",
            motet_id="production",
            principal_id="user-123"
        )
        assert visibility == Visibility.USER
        assert lifecycle == LifecycleDuration.PERMANENT
    
    def test_resolve_motet_visibility(self):
        """Test resolution to MOTET visibility when tenant_id and motet_id present."""
        visibility, lifecycle = resolve_visibility_and_lifecycle(
            tenant_id="acme-corp",
            motet_id="production"
        )
        assert visibility == Visibility.MOTET
        assert lifecycle == LifecycleDuration.PERMANENT
    
    def test_resolve_tenant_visibility(self):
        """Test resolution to TENANT visibility when only tenant_id present."""
        visibility, lifecycle = resolve_visibility_and_lifecycle(
            tenant_id="acme-corp"
        )
        assert visibility == Visibility.TENANT
        assert lifecycle == LifecycleDuration.PERMANENT
    
    def test_resolve_global_visibility(self):
        """Test resolution to GLOBAL visibility when no IDs present."""
        visibility, lifecycle = resolve_visibility_and_lifecycle()
        assert visibility == Visibility.GLOBAL
        assert lifecycle == LifecycleDuration.PERMANENT
    
    def test_resolve_conversation_lifecycle(self):
        """Test resolution to CONVERSATION lifecycle when conversation_id present."""
        visibility, lifecycle = resolve_visibility_and_lifecycle(
            tenant_id="acme-corp",
            motet_id="production",
            principal_id="user-123",
            conversation_id="conv-abc123"
        )
        assert visibility == Visibility.USER
        assert lifecycle == LifecycleDuration.CONVERSATION
    
    def test_resolve_task_lifecycle(self):
        """Test resolution to TASK lifecycle when task_id present."""
        visibility, lifecycle = resolve_visibility_and_lifecycle(
            tenant_id="acme-corp",
            motet_id="production",
            principal_id="user-123",
            task_id="task-xyz789"
        )
        assert visibility == Visibility.USER
        assert lifecycle == LifecycleDuration.TASK
    
    def test_resolve_session_lifecycle(self):
        """Test resolution to SESSION lifecycle when session_id present."""
        visibility, lifecycle = resolve_visibility_and_lifecycle(
            tenant_id="acme-corp",
            motet_id="production",
            principal_id="user-123",
            session_id="session-456"
        )
        assert visibility == Visibility.USER
        assert lifecycle == LifecycleDuration.SESSION


class TestInstanceSpecValidation:
    """Test instance specification validation (ADR-0058)."""
    
    def test_valid_stateless_shared(self):
        """Test valid stateless shared instance spec."""
        validate_instance_spec(
            state_model=StateModel.STATELESS,
            credential_scope=CredentialScope.MOTET,
            visibility=Visibility.MOTET,
            lifecycle_duration=LifecycleDuration.PERMANENT,
            shared_state_allowed=False
        )
        # Should not raise
    
    def test_valid_stateful_user(self):
        """Test valid stateful user instance spec."""
        validate_instance_spec(
            state_model=StateModel.STATEFUL,
            credential_scope=CredentialScope.USER,
            visibility=Visibility.USER,
            lifecycle_duration=LifecycleDuration.CONVERSATION,
            shared_state_allowed=False
        )
        # Should not raise
    
    def test_invalid_stateful_non_user_visibility(self):
        """Test invalid stateful instance with non-USER visibility."""
        with pytest.raises(ValueError, match="Stateful services with MOTET/TENANT/GLOBAL visibility require shared_state_allowed=True"):
            validate_instance_spec(
                state_model=StateModel.STATEFUL,
                credential_scope=CredentialScope.MOTET,
                visibility=Visibility.MOTET,
                lifecycle_duration=LifecycleDuration.PERMANENT,
                shared_state_allowed=False
            )
    
    def test_invalid_credential_scope_mismatch(self):
        """Test invalid credential scope vs visibility mismatch."""
        with pytest.raises(ValueError, match="User credential scope requires USER visibility"):
            validate_instance_spec(
                state_model=StateModel.STATELESS,
                credential_scope=CredentialScope.USER,
                visibility=Visibility.MOTET,
                lifecycle_duration=LifecycleDuration.PERMANENT,
                shared_state_allowed=False
            )
    
    def test_invalid_global_credential_with_user_visibility(self):
        """Test invalid GLOBAL credential scope with USER visibility."""
        with pytest.raises(ValueError, match="Global credential scope is invalid with USER visibility"):
            validate_instance_spec(
                state_model=StateModel.STATELESS,
                credential_scope=CredentialScope.GLOBAL,
                visibility=Visibility.USER,
                lifecycle_duration=LifecycleDuration.PERMANENT,
                shared_state_allowed=False
            )


class TestMessageValidation:
    """Test message validation and error handling."""
    
    def test_request_message_missing_fields(self):
        """Test MCPRequestMessage validation with missing required fields."""
        with pytest.raises(ValueError):
            MCPRequestMessage()  # Missing required fields
    
    def test_response_message_without_request_id(self):
        """Test MCPResponseMessage validation."""
        with pytest.raises(ValueError):
            MCPResponseMessage(
                service_id="test",
                instance_key="test:global",
                # Missing request_id
                jsonrpc_response={"jsonrpc": "2.0", "id": "test", "result": {}}
            )
    
    def test_log_message_minimal_fields(self):
        """Test MCPLogMessage with minimal required fields."""
        log_msg = MCPLogMessage(
            service_id="test",
            instance_key="test:global",
            message="Test log message"
        )
        
        assert log_msg.service_id == "test"
        assert log_msg.message == "Test log message"
        assert log_msg.level == "info"  # Default value
        assert log_msg.request_id is None  # Optional field
        assert log_msg.raw_stderr is None  # Optional field


class TestEdgeCases:
    """Test edge cases and boundary conditions."""
    
    def test_empty_service_id(self):
        """Test handling of empty service_id."""
        msg = MCPRequestMessage(
            service_id="",
            instance_key=":global",
            jsonrpc_request={"jsonrpc": "2.0", "id": "test", "method": "test"}
        )
        assert msg.service_id == ""
    
    def test_very_long_instance_key(self):
        """Test handling of very long instance_key."""
        long_key = "service:" + ":".join(["part"] * 100)
        stream_name = generate_stream_name(
            "service", Visibility.GLOBAL, long_key, StreamType.REQUESTS
        )
        
        parsed = parse_stream_name(stream_name)
        assert "instance_key" in parsed
    
    def test_special_characters_in_instance_key(self):
        """Test handling of special characters in instance_key."""
        instance_key = "service:tenant@example.com:motet_prod:user-123"
        stream_name = generate_stream_name(
            "service", Visibility.USER, instance_key, StreamType.REQUESTS
        )
        
        parsed = parse_stream_name(stream_name)
        assert "instance_key" in parsed
    
    def test_unicode_in_log_message(self):
        """Test handling of unicode characters in log messages."""
        log_msg = MCPLogMessage(
            service_id="test",
            instance_key="test:global",
            message="Test message with unicode: 🚀 ✅ 🔧",
            raw_stderr="Raw stderr with unicode: 📊 💾 🌐"
        )
        
        assert "🚀" in log_msg.message
        assert "📊" in log_msg.raw_stderr
        
        # Test JSON serialization with unicode
        json_str = log_msg.model_dump_json()
        parsed = json.loads(json_str)
        assert "🚀" in parsed["message"]
        assert "📊" in parsed["raw_stderr"]


if __name__ == "__main__":
    pytest.main([__file__])
