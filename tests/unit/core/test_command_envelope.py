"""
Motet - Command Envelope Contract Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-23

Description:
    Unit tests for the ADR-0133 command envelope: wrap as BaseCommandResponse,
    unwrap via model_validate, transport ``{status: completed, result}`` stays
    inside the invoker, and workflow-shaped domain ``status: completed`` is data.

Usage:
    pytest tests/unit/core/test_command_envelope.py
"""

from unittest.mock import Mock, patch

import pytest
from pydantic import Field

from motet.core.commands.base_command_data import BaseCommandData
from motet.core.commands.decorator import MotetContext, distributed_command
from motet.core.commands.motet_context import unwrap_child_envelope
from motet.core.commands.response_models import (
    CommandExecutionError,
    GatherExecutionError,
    child_command_envelope,
    parse_command_envelope,
    strip_transport_envelope,
)


class EnvelopeTestData(BaseCommandData):
    value: str = Field(default="x")


@distributed_command()
def _envelope_do_target_cmd(data: EnvelopeTestData, motet: MotetContext) -> dict:
    return {"unused": True}


def _metadata(**kwargs: object) -> dict:
    base = {
        "command_id": "cmd-1",
        "command_type": "envelope_test",
        "execution_time_ms": 1.0,
    }
    base.update(kwargs)
    return base


def test_parse_command_envelope_rejects_workflow_completed_payload() -> None:
    with pytest.raises(Exception):
        parse_command_envelope(
            {"status": "completed", "step_results": [{"ok": True}]}
        )


def test_strip_transport_leaves_workflow_domain_payload() -> None:
    payload = {"status": "completed", "step_results": [{"ok": True}]}
    assert strip_transport_envelope(payload) is payload


def test_strip_transport_peels_invoker_completed_result() -> None:
    inner = {
        "status": "success",
        "data": {"hello": "world"},
        "metadata": _metadata(),
        "warnings": [],
    }
    outer = {"status": "completed", "result": inner, "execution_id": "exec-1"}
    assert strip_transport_envelope(outer) == inner


def test_decorator_wraps_workflow_shaped_payload() -> None:
    @distributed_command()
    def envelope_workflow_shaped(data: EnvelopeTestData, motet: MotetContext) -> dict:
        motet.add_warning("wrapped")
        return {"status": "completed", "step_results": [{"ok": True}]}

    cmd = envelope_workflow_shaped(
        data=EnvelopeTestData(value="x"),
        task_id="t",
        conversation_id="c",
    )
    raw = cmd._do_execute({})
    envelope = parse_command_envelope(raw)
    assert envelope.status == "success"
    assert envelope.data["status"] == "completed"
    assert envelope.data["step_results"] == [{"ok": True}]
    assert "wrapped" in envelope.warnings


def test_decorator_raise_becomes_error_envelope() -> None:
    @distributed_command()
    def envelope_raises(data: EnvelopeTestData, motet: MotetContext) -> dict:
        raise ValueError("boom")

    cmd = envelope_raises(
        data=EnvelopeTestData(value="x"),
        task_id="t",
        conversation_id="c",
    )
    raw = cmd._do_execute({})
    envelope = parse_command_envelope(raw)
    assert envelope.status == "error"
    assert envelope.error is not None
    assert envelope.error.type == "ValueError"
    assert envelope.error.message == "boom"


def test_do_unwraps_workflow_payload_from_success_envelope() -> None:
    motet = MotetContext(task_id="t", command_id="c", worker_context={})
    domain = {"status": "completed", "step_results": [{"ok": True}]}
    with patch("motet.core.workers.invoker_context.get_distributed_invoker") as mock_get:
        mock_invoker = Mock()
        mock_get.return_value = mock_invoker
        mock_invoker.execute_command.return_value = {
            "status": "completed",
            "result": {
                "status": "success",
                "data": domain,
                "metadata": _metadata(),
                "warnings": [],
            },
        }
        data = motet.do(_envelope_do_target_cmd, data=EnvelopeTestData())
        assert data == domain
        assert motet.last_metadata is not None
        assert motet.last_metadata.command_type == "envelope_test"


def test_do_raises_on_error_envelope() -> None:
    motet = MotetContext(task_id="t", command_id="c", worker_context={})
    with patch("motet.core.workers.invoker_context.get_distributed_invoker") as mock_get:
        mock_invoker = Mock()
        mock_get.return_value = mock_invoker
        mock_invoker.execute_command.return_value = {
            "status": "completed",
            "result": {
                "status": "error",
                "data": None,
                "error": {
                    "type": "RuntimeError",
                    "message": "nope",
                    "details": {},
                    "recoverable": False,
                    "retry_recommended": False,
                },
                "metadata": _metadata(),
                "warnings": [],
            },
        }
        with pytest.raises(CommandExecutionError) as exc_info:
            motet.do(_envelope_do_target_cmd, data=EnvelopeTestData())
        assert exc_info.value.error_type == "RuntimeError"
        assert exc_info.value.message == "nope"


def test_do_does_not_treat_bare_completed_as_command_failure() -> None:
    """Transport-stripped domain {status: completed} is not an envelope."""
    motet = MotetContext(task_id="t", command_id="c", worker_context={})
    with patch("motet.core.workers.invoker_context.get_distributed_invoker") as mock_get:
        mock_invoker = Mock()
        mock_get.return_value = mock_invoker
        mock_invoker.execute_command.return_value = {
            "status": "completed",
            "step_results": [{"ok": True}],
        }
        with pytest.raises(CommandExecutionError) as exc_info:
            motet.do(_envelope_do_target_cmd, data=EnvelopeTestData())
        assert exc_info.value.error_type == "EnvelopeValidationError"


def test_composition_errors_are_sdk_types() -> None:
    from motet.core.bundles.bundle_reload import motet_sdk_runtime_bridge
    from motet.core.commands.response_models import (
        ApplyExecutionError,
        GatherExecutionError,
    )

    with motet_sdk_runtime_bridge():
        from motet_sdk.models import (
            ApplyExecutionError as SdkApply,
            CommandExecutionError as SdkDo,
            GatherExecutionError as SdkJoin,
        )

        assert CommandExecutionError is SdkDo
        assert GatherExecutionError is SdkJoin
        assert ApplyExecutionError is SdkApply


def test_child_command_envelope_round_trips_parse() -> None:
    success = child_command_envelope(
        command_id="ocr-1",
        command_type="core.ocr_image_page",
        data={"page_num": 1, "text": "page one"},
    )
    envelope = parse_command_envelope(success)
    assert envelope.status == "success"
    assert envelope.data == {"page_num": 1, "text": "page one"}
    assert unwrap_child_envelope(success) == {"page_num": 1, "text": "page one"}

    failed = child_command_envelope(
        command_id="ocr-2",
        command_type="core.ocr_image_page",
        error={"type": "TimeoutError", "message": "ocr timed out", "details": {"page": 3}},
    )
    assert unwrap_child_envelope(failed) == {
        "_error": True,
        "error_type": "TimeoutError",
        "message": "ocr timed out",
        "details": {"page": 3},
    }


def test_unwrap_child_does_not_sniff_slim_map_dicts() -> None:
    """Slim ``{status, data}`` is not an envelope; the producer must emit metadata."""
    slim = {
        "command_id": "ocr-1",
        "status": "success",
        "data": {"page_num": 1, "text": "page one"},
    }
    assert unwrap_child_envelope(slim) == slim


def test_unwrap_child_leaves_workflow_domain_payload() -> None:
    domain = {"status": "completed", "step_results": [{"ok": True}]}
    assert unwrap_child_envelope(domain) == domain


def test_apply_unwraps_map_ocr_children() -> None:
    motet = MotetContext(task_id="t", command_id="c", worker_context={})
    with patch("motet.core.workers.invoker_context.get_distributed_invoker") as mock_get:
        mock_invoker = Mock()
        mock_get.return_value = mock_invoker
        mock_invoker.execute_command.return_value = {
            "status": "completed",
            "result": {
                "status": "success",
                "data": {
                    "results": [
                        child_command_envelope(
                            command_id="ocr-1",
                            command_type="core.ocr_image_page",
                            data={"page_num": 1, "text": "page one"},
                        ),
                        child_command_envelope(
                            command_id="ocr-2",
                            command_type="core.ocr_image_page",
                            data={"page_num": 2, "text": "page two"},
                        ),
                    ],
                    "successful": 2,
                    "failed": 0,
                },
                "metadata": _metadata(command_type="core.map"),
                "warnings": [],
            },
        }
        pages = motet.apply(_envelope_do_target_cmd, inputs=[{}, {}])
        assert pages == [
            {"page_num": 1, "text": "page one"},
            {"page_num": 2, "text": "page two"},
        ]


def test_join_unwraps_partial_results_on_failure() -> None:
    """Authors recovering from GatherExecutionError must not re-unwrap envelopes."""
    motet = MotetContext(task_id="t", command_id="c", worker_context={})
    with patch("motet.core.workers.invoker_context.get_distributed_invoker") as mock_get:
        mock_invoker = Mock()
        mock_get.return_value = mock_invoker
        mock_invoker.execute_command.return_value = {
            "status": "completed",
            "result": {
                "status": "partial_success",
                "data": {
                    "results": [
                        child_command_envelope(
                            command_id="a-1",
                            command_type="core.agent_loop",
                            data={"final_response": "found it", "tool_results": []},
                        ),
                        child_command_envelope(
                            command_id="a-2",
                            command_type="core.agent_loop",
                            error={
                                "type": "TimeoutError",
                                "message": "worker timed out",
                                "details": {},
                            },
                        ),
                    ],
                    "successful": 1,
                    "failed": 1,
                },
                "error": {
                    "type": "PartialGroupFailure",
                    "message": "1 of 2 commands failed",
                    "details": {},
                    "recoverable": True,
                },
                "metadata": _metadata(command_type="core.gather"),
                "warnings": [],
            },
        }
        with pytest.raises(GatherExecutionError) as exc_info:
            motet.join(
                [(_envelope_do_target_cmd, EnvelopeTestData()), (_envelope_do_target_cmd, EnvelopeTestData())],
                fail_fast=False,
            )
        assert exc_info.value.partial_results == [
            {"final_response": "found it", "tool_results": []},
            {
                "_error": True,
                "error_type": "TimeoutError",
                "message": "worker timed out",
                "details": {},
            },
        ]


def test_remaining_command_timeout_uses_started_at() -> None:
    from datetime import datetime, timedelta

    from motet.core.commands.motet_context import remaining_command_timeout_seconds

    command = Mock()
    command.distribution_started_at = datetime.utcnow() - timedelta(seconds=40)
    command.distributed_context = Mock(timeout_seconds=100, created_at=None)
    remaining = remaining_command_timeout_seconds(command)
    assert remaining is not None
    assert 55 <= remaining <= 65


def test_remaining_command_timeout_missing_command() -> None:
    from motet.core.commands.motet_context import remaining_command_timeout_seconds

    assert remaining_command_timeout_seconds(None) is None


def test_motet_context_hides_transport_and_envelope_builders() -> None:
    assert not hasattr(MotetContext, "call")
    assert not hasattr(MotetContext, "gather")
    assert not hasattr(MotetContext, "map")
    assert not hasattr(MotetContext, "create_response")
    assert not hasattr(MotetContext, "create_error")
    assert hasattr(MotetContext, "_call")
    assert hasattr(MotetContext, "do")
    assert hasattr(MotetContext, "add_warning")
