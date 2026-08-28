import base64
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict
from unittest.mock import patch

from motet.core.orchestration.scheduling.models import ScheduleMetadata, ScheduleStatus, ScheduleType
from motet.core.orchestration.scheduling.storage import ScheduleStorage


class DummyEncryptionService:
    """Lightweight encryption wrapper used to bypass Vault for tests."""

    def wrap_key(self, dek: bytes, tenant_id: str) -> Dict[str, Any]:
        return {
            "wrapped_key": base64.b64encode(dek).decode("utf-8"),
            "iv": base64.b64encode(b"0123456789ab").decode("utf-8"),
            "tenant_id": tenant_id,
            "encryption_version": "aes-256-gcm-v1",
        }

    def unwrap_key(self, wrapped_blob: Dict[str, Any]) -> bytes:
        return base64.b64decode(wrapped_blob["wrapped_key"])


def _build_schedule(**overrides: Any) -> ScheduleMetadata:
    defaults = dict(
        schedule_id="sch-123",
        command_id="cmd-123",
        command_type="agent_turn",
        schedule_type=ScheduleType.DELAYED,
        scheduled_at=datetime.now(timezone.utc),
        tenant_id="tenant-abc",
        created_by="principal-xyz",
        metadata={"original_command_data": {"foo": "bar"}},
        condition_expression="input.score > 0.9",
        last_error="previous failure",
    )
    defaults.update(overrides)
    return ScheduleMetadata(**defaults)


@patch.object(ScheduleStorage, "_add_to_type_sets")
@patch("motet.core.orchestration.scheduling.storage.store_structured_data_sync")
def test_store_schedule_encrypts_sensitive_fields(mock_store, mock_add_sets):
    storage = ScheduleStorage()
    storage._encryption_service = DummyEncryptionService()

    schedule = _build_schedule()
    assert storage.store_schedule(schedule) is True

    stored_data = mock_store.call_args[0][2]
    assert stored_data["metadata"] is None
    assert stored_data["condition_expression"] is None
    assert stored_data["last_error"] is None
    assert "_sensitive_envelope" in stored_data

    envelope = stored_data["_sensitive_envelope"]
    assert envelope["encrypted"] is True
    assert envelope["encryption_mode"] == "envelope-v1"
    assert "dek" in envelope


@patch.object(ScheduleStorage, "_add_to_type_sets")
@patch("motet.core.orchestration.scheduling.storage.retrieve_structured_data_sync")
@patch("motet.core.orchestration.scheduling.storage.store_structured_data_sync")
def test_retrieve_schedule_decrypts_sensitive_fields(
    mock_store, mock_retrieve, mock_add_sets
):
    storage = ScheduleStorage()
    storage._encryption_service = DummyEncryptionService()

    schedule = _build_schedule(
        metadata={"original_command_data": {"messages": ["hello"]}},
        condition_expression="context.ready",
        last_error="timeout",
    )
    storage.store_schedule(schedule)

    stored_payload = deepcopy(mock_store.call_args[0][2])
    mock_retrieve.return_value = stored_payload

    loaded = storage.retrieve_schedule(schedule.schedule_id)
    assert isinstance(loaded, ScheduleMetadata)
    assert loaded.metadata == {"original_command_data": {"messages": ["hello"]}}
    assert loaded.condition_expression == "context.ready"
    assert loaded.last_error == "timeout"
    assert loaded.status == ScheduleStatus.ACTIVE

