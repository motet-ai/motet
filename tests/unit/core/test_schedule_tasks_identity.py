from motet.core.workers.schedule_tasks import _resolve_scheduled_identity


def test_resolve_scheduled_identity_prefers_schedule_creator():
    value = _resolve_scheduled_identity(
        "user-123",
        {"principal_id": "other-user"},
    )
    assert value == "user-123"


def test_resolve_scheduled_identity_falls_back_to_envelope_principal():
    import pytest

    with pytest.raises(ValueError, match="missing created_by"):
        _resolve_scheduled_identity(
            None,
            {"principal_id": "envelope-user"},
        )
