"""
Motet - Artifact RAG End-to-End Integration Test

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-05-06

Description:
    Exercises the ADR-0063 text artifact RAG flow across real Redis artifact
    storage, the ADR-0107 HTTP embedding service, Valkey Search indexing, scoped
    retrieval, and RagContextProvider context injection.

Dependencies:
    - pytest for Docker-backed integration execution
    - fastapi.testclient for HTTP upload/chat coverage
    - motet.core.artifacts for source and derived artifact storage
    - motet.core.embedding for sibling-server embedding calls
    - motet.core.commands.builtin.rag for indexing and retrieval commands
    - motet.core.orchestration.orchestrator for HTTP chat streaming through agent_turn
    - motet.core.orchestration.context.rag_context for final context injection

Usage:
    docker compose -f tests/docker-compose.test.yml --profile workers up -d embedding-server
    docker compose -f tests/docker-compose.test.yml run --rm \
      -e MOTET_EMBEDDING_TOPOLOGY=sibling \
      -e MOTET_EMBEDDING_ENDPOINT=http://embedding-server:8091 \
      test-runner python -m pytest tests/integration/test_artifact_rag_e2e.py -v

Notes:
    - The test skips when no embedding endpoint is configured or reachable.
    - The production-like HTTP test runs decorated command execution in-process
      to keep orchestration deterministic without requiring Celery workers.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from motet.core.artifacts import ArtifactKind, RedisArtifactStore, ScopedArtifactStore
from motet.core.distributed.redis_manager import get_sync_redis_client
from motet.core.embedding import create_embedding_service
from motet.core.commands.builtin import artifacts as artifact_commands
from motet.core.commands.builtin import derivation as derivation_commands
from motet.core.commands.builtin import rag as rag_commands
from motet.core.commands.command_data_classes import (
    DeriveUploadTextData,
    PrepareArtifactIndexData,
    RagRetrieveContextData,
)
from motet.core.orchestration.context.rag_context import RagContextProvider
from motet.core.orchestration.context.types import ContextPipelineState
from motet.core.rag import ArtifactChunkRepository
from motet.core.types import Message, Response, TextPart
from motet.interfaces.http import create_app


class _LoggerStub:
    def warning(self, *args: Any, **kwargs: Any) -> None:
        return None


class _OrchestratorStub:
    def set_role(self, role: str) -> None:
        return None


def _parse_sse_events(body: str) -> list[dict[str, Any]]:
    """Parse the small subset of SSE frames used by the chat endpoint."""

    events: list[dict[str, Any]] = []
    current_event: str | None = None
    current_data: list[str] = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            if current_event:
                payload = "\n".join(current_data)
                try:
                    data = json.loads(payload) if payload else {}
                except json.JSONDecodeError:
                    data = payload
                events.append({"event": current_event, "data": data})
            current_event = None
            current_data = []
            continue
        if line.startswith("event:"):
            current_event = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            current_data.append(line.split(":", 1)[1].strip())

    if current_event:
        payload = "\n".join(current_data)
        try:
            data = json.loads(payload) if payload else {}
        except json.JSONDecodeError:
            data = payload
        events.append({"event": current_event, "data": data})
    return events


def _wrapped(command_fn: Any) -> Any:
    """Return the undecorated command body when the command decorator is present."""

    return getattr(command_fn, "__wrapped__", command_fn)


def _require_embedding_endpoint() -> str:
    endpoint = os.getenv("MOTET_EMBEDDING_ENDPOINT", "").strip().rstrip("/")
    if not endpoint:
        pytest.skip("MOTET_EMBEDDING_ENDPOINT is required for artifact RAG E2E")
    try:
        urllib.request.urlopen(f"{endpoint}/healthz", timeout=5).read()
    except Exception as e:
        pytest.skip(f"Embedding endpoint is not reachable for artifact RAG E2E: {e}")
    return endpoint


@pytest.mark.integration
@pytest.mark.e2e
@pytest.mark.requires_redis
def test_text_artifact_rag_indexes_retrieves_and_injects_context(monkeypatch: pytest.MonkeyPatch) -> None:
    _require_embedding_endpoint()

    run_id = uuid.uuid4().hex
    tenant_id = f"tenant-rag-e2e-{run_id}"
    principal_id = f"principal-rag-e2e-{run_id}"
    motet_id = "motet-rag-e2e"
    conversation_id = f"conversation-rag-e2e-{run_id}"
    service_name = f"artifact_store_rag_e2e_{run_id}"

    raw_store = RedisArtifactStore(service_name=service_name)
    scoped_store = ScopedArtifactStore(
        raw_store,
        tenant_id=tenant_id,
        principal_id=principal_id,
        motet_id=motet_id,
    )
    embedding_service = create_embedding_service(topology="sibling")
    repository = ArtifactChunkRepository(embedding_dim=embedding_service.get_embedding_dimension())

    cfg = SimpleNamespace(
        artifact_rag_enabled=True,
        artifact_rag_top_k=3,
        artifact_rag_similarity_threshold=0.0,
        artifact_rag_chunk_size=3200,
        artifact_rag_chunk_overlap=400,
        artifact_rag_token_budget=1200,
    )

    class _MotetStub:
        def __init__(self) -> None:
            self.tenant_id = tenant_id
            self.principal_id = principal_id
            self.motet_id = motet_id
            self.conversation_id = conversation_id
            self.task_id = f"task-{run_id}"
            self.command_id = f"command-{run_id}"
            self.stack = SimpleNamespace(config=cfg)
            self.artifact_store = scoped_store
            self._worker_context = {"embedding_service": embedding_service}

        def log_fields(self, **extra: Any) -> dict[str, Any]:
            return {
                "tenant_id": self.tenant_id,
                "principal_id": self.principal_id,
                "motet_id": self.motet_id,
                "conversation_id": self.conversation_id,
                **extra,
            }

        def do(self, command: Any, data: Any) -> dict[str, Any]:
            assert command is rag_commands.rag_retrieve_context
            return _wrapped(rag_commands.rag_retrieve_context)(data)

    motet = _MotetStub()
    monkeypatch.setattr(rag_commands, "get_motet_context", lambda: motet)

    source_id = ""
    derived_id = ""
    try:
        source_id = scoped_store.put(
            payload=b"Original upload bytes for the Neptune launch archive.",
            content_type="text/plain",
            kind=ArtifactKind.USER_UPLOAD,
            metadata={
                "filename": "neptune-launch.txt",
                "conversation_id": conversation_id,
                "role": "user",
            },
            ttl_seconds=300,
        )
        derived_id = scoped_store.put(
            payload=(
                "The Neptune launch budget is 42 million credits. "
                "The approved launch window is dawn on Sol 12. "
                "Unrelated catering notes mention basil sandwiches."
            ),
            content_type="text/plain",
            kind=ArtifactKind.DERIVED_TEXT,
            source_artifact_id=source_id,
            metadata={
                "source_filename": "neptune-launch.txt",
                "conversation_id": conversation_id,
                "role": "user",
            },
            ttl_seconds=300,
        )

        index_result = _wrapped(rag_commands.prepare_artifact_index)(
            PrepareArtifactIndexData(
                source_artifact_id=source_id,
                derived_artifact_id=derived_id,
                force_reindex=True,
            )
        )
        assert index_result["chunks_indexed"] >= 1

        retrieve_result = _wrapped(rag_commands.rag_retrieve_context)(
            RagRetrieveContextData(
                query_text="What is the Neptune launch budget?",
                scope="conversation",
                conversation_id=conversation_id,
                role="user",
                top_k=3,
                token_budget=1200,
            )
        )
        assert retrieve_result["chunk_count"] >= 1
        assert "Neptune launch budget" in retrieve_result["context_text"]
        assert retrieve_result["chunks"][0]["source_artifact_id"] == source_id

        message = Message(
            role="user",
            content="What is the Neptune launch budget?",
            content_parts=[
                TextPart(text="<attachment artifact_id='derived'>full document fallback</attachment>"),
                TextPart(text="What is the Neptune launch budget?"),
            ],
        )
        state = ContextPipelineState(messages=[message])

        out = RagContextProvider().apply(state, data=SimpleNamespace(), motet=motet, logger=_LoggerStub())

        parts = out.messages[0].content_parts or []
        texts = [part.text for part in parts if getattr(part, "type", None) == "text"]
        assert out.context_info["rag_context_enabled"] is True
        assert out.context_info["artifact_rag_chunk_count"] >= 1
        assert texts[0].startswith("<artifact_rag_context>")
        assert "Neptune launch budget" in texts[0]
        assert all("<attachment " not in text for text in texts)
    finally:
        if source_id:
            repository.delete_source_chunks(tenant_id=tenant_id, source_artifact_id=source_id)
        if derived_id:
            scoped_store.delete(derived_id)
        if source_id:
            scoped_store.delete(source_id)
        try:
            repository._redis.execute_command("FT.DROPINDEX", repository.index_name(tenant_id), "DD")
        except Exception:
            pass
        time.sleep(0.01)


@pytest.mark.integration
@pytest.mark.e2e
@pytest.mark.requires_redis
def test_http_upload_to_streaming_chat_uses_real_agent_turn_with_artifact_citation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_embedding_endpoint()

    run_id = uuid.uuid4().hex
    tenant_id = f"tenant-rag-real-http-{run_id}"
    principal_id = f"principal-rag-real-http-{run_id}"
    motet_id = "motet-rag-real-http"
    conversation_id = f"conversation-rag-real-http-{run_id}"
    service_name = f"artifact_store_rag_real_http_{run_id}"

    raw_store = RedisArtifactStore(service_name=service_name)
    scoped_store = ScopedArtifactStore(
        raw_store,
        tenant_id=tenant_id,
        principal_id=principal_id,
        motet_id=motet_id,
    )
    embedding_service = create_embedding_service(topology="sibling")
    repository = ArtifactChunkRepository(embedding_dim=embedding_service.get_embedding_dimension())
    redis_client = get_sync_redis_client(f"artifact_rag_real_http_{run_id}")
    created_artifact_ids: list[str] = []

    cfg = SimpleNamespace(
        artifact_rag_enabled=True,
        artifact_rag_index_on_derivation=True,
        artifact_rag_top_k=3,
        artifact_rag_similarity_threshold=0.0,
        artifact_rag_chunk_size=3200,
        artifact_rag_chunk_overlap=400,
        artifact_rag_token_budget=1200,
        artifact_rag_hybrid_enabled=True,
        artifact_rag_vector_weight=0.7,
        artifact_rag_lexical_weight=0.3,
        artifact_rag_candidate_multiplier=4,
        memory_backend="redis",
    )
    stack_holder: dict[str, Any] = {}

    class _InProcessInvoker:
        def execute_command(self, command: Any) -> dict[str, Any]:
            command_type = command.get_command_type()
            if command_type == "core.dispatch":
                children = command._deserialize_child_commands(self._worker_context())
                dispatched: list[str] = []
                for child in children:
                    dispatched.append(child.command_id)
                    self.execute_command(child)
                return {
                    "status": "completed",
                    "result": {
                        "status": "success",
                        "data": {"dispatched": dispatched, "total_commands": len(dispatched)},
                    },
                }

            result = command._do_execute(self._worker_context())
            data = result.get("data") if isinstance(result, dict) else None
            if command_type == "core.create_artifact" and isinstance(data, dict):
                artifact_id = str(data.get("artifact_id") or "")
                if artifact_id:
                    created_artifact_ids.append(artifact_id)
            elif command_type == "core.derive_upload_text" and isinstance(data, dict):
                artifact_id = str(data.get("id") or "")
                if artifact_id:
                    created_artifact_ids.append(artifact_id)
            return {"status": "completed", "result": result}

        def _worker_context(self) -> dict[str, Any]:
            stack = stack_holder.get("stack") or SimpleNamespace(config=cfg, tool_registry=None)
            return {
                "redis": redis_client,
                "distributed_invoker": self,
                "embedding_service": embedding_service,
                "stack": stack,
                "tool_registry": getattr(stack, "tool_registry", None),
                "memory_manager": getattr(stack, "memory", None),
                "event_bus": getattr(stack, "event_bus", None),
                "observer_manager": getattr(stack, "observer_manager", None),
            }

    in_process_invoker = _InProcessInvoker()

    def _citation_mock_response(messages: list[Any]) -> str:
        texts: list[str] = []
        for message in messages:
            texts.append(str(getattr(message, "content", "") or ""))
            for part in list(getattr(message, "content_parts", None) or []):
                text = getattr(part, "text", None)
                if isinstance(text, str):
                    texts.append(text)
        joined = "\n".join(texts)
        if "Orion risk register" in joined and "escrow rotation every 30 days" in joined:
            return (
                "The uploaded Orion risk register requires escrow rotation every 30 days. "
                "Citation: orion-risk-register.txt."
            )
        return "No artifact citation was available."

    monkeypatch.setenv("MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS", "true")
    monkeypatch.setenv("MOTET_MODEL_PROVIDER", "mock")
    monkeypatch.setenv("MOTET_MODEL_NAME", "mock-small")
    monkeypatch.setenv("MOTET_ARTIFACT_RAG_ENABLED", "true")
    monkeypatch.setenv("MOTET_ARTIFACT_RAG_INDEX_ON_DERIVATION", "true")
    monkeypatch.setattr("motet.core.artifacts.get_artifact_store", lambda: raw_store)
    monkeypatch.setattr("motet.core.media.derivation_service.get_artifact_store", lambda: raw_store)
    monkeypatch.setattr("motet.core.workers.global_invoker.execute_command", in_process_invoker.execute_command)
    monkeypatch.setattr(
        "motet.core.models.adapters.providers.mock._mock_response",
        _citation_mock_response,
    )

    import motet.core as core_module

    original_stack_cls = core_module.MotetStack

    def _tracked_stack(config: Any) -> Any:
        for key, value in vars(cfg).items():
            if hasattr(config, key):
                setattr(config, key, value)
        config.model_provider = "mock"
        config.model_name = "mock-small"
        stack = original_stack_cls(config)
        stack_holder["stack"] = stack
        return stack

    monkeypatch.setattr(core_module, "MotetStack", _tracked_stack)

    app = create_app()
    headers = {
        "X-Principal-Id": principal_id,
        "X-Tenant-Id": tenant_id,
        "X-Motet-Id": motet_id,
    }
    source_id = ""
    try:
        with TestClient(app) as client:
            upload = client.post(
                "/api/v1/artifacts",
                headers=headers,
                params={"kind": "user_upload", "conversation_id": conversation_id},
                files={
                    "file": (
                        "orion-risk-register.txt",
                        (
                            "The Orion risk register says the required mitigation is "
                            "escrow rotation every 30 days. Catering notes mention mint tea."
                        ).encode("utf-8"),
                        "text/plain",
                    )
                },
            )
            assert upload.status_code == 200, upload.text
            source_id = upload.json()["artifact_id"]

            status = client.get(
                "/api/v1/artifacts/indexing-status",
                headers=headers,
                params=[("artifact_id", source_id)],
            )
            assert status.status_code == 200, status.text
            items = status.json()["items"]
            assert items and items[0]["summary"] == "indexed"
            assert items[0]["total_chunks_indexed"] >= 1

            with client.stream(
                "POST",
                "/api/v1/chat",
                headers=headers,
                json={
                    "messages": [
                        {
                            "role": "user",
                            "content": "What mitigation does the uploaded Orion document require?",
                            "attachments": [
                                {
                                    "artifact_id": source_id,
                                    "filename": "orion-risk-register.txt",
                                    "content_type": "text/plain",
                                    "bytes": 128,
                                }
                            ],
                        }
                    ],
                    "conversation_id": conversation_id,
                    "stream": True,
                    "artifact_rag_scope": "conversation",
                    "overrides": {
                        "model_provider": "mock",
                        "model_name": "mock-small",
                    },
                },
            ) as chat:
                assert chat.status_code == 200, chat.text
                assert "text/event-stream" in chat.headers.get("content-type", "")
                events = _parse_sse_events(chat.read().decode("utf-8"))

        token_text = "".join(
            event["data"].get("t", "")
            for event in events
            if event["event"] == "token" and isinstance(event["data"], dict)
        )
        end_events = [event for event in events if event["event"] == "end"]

        assert any(
            event["event"] == "agent_turn_start"
            and isinstance(event["data"], dict)
            and event["data"].get("agent_id") == "core.default"
            for event in events
        )
        assert "escrow rotation every 30 days" in token_text
        assert "Citation: orion-risk-register.txt" in token_text
        assert end_events
        assert end_events[-1]["data"].get("content")
        citations = end_events[-1]["data"].get("artifact_rag_citations")
        assert isinstance(citations, list)
        assert citations[0]["source_label"] == "orion-risk-register.txt"
        assert citations[0]["artifact_id"] == source_id
        assert citations[0]["api_path"] == f"/api/v1/artifacts/{source_id}"
    finally:
        for artifact_id in reversed(created_artifact_ids):
            scoped_store.delete(artifact_id)
        if source_id:
            repository.delete_source_chunks(tenant_id=tenant_id, source_artifact_id=source_id)
        try:
            repository._redis.execute_command("FT.DROPINDEX", repository.index_name(tenant_id), "DD")
        except Exception:
            pass
        try:
            redis_client.close()
        except Exception:
            pass
        time.sleep(0.01)


@pytest.mark.integration
@pytest.mark.e2e
@pytest.mark.requires_redis
def test_http_upload_to_chat_uses_artifact_rag_context(monkeypatch: pytest.MonkeyPatch) -> None:
    _require_embedding_endpoint()

    run_id = uuid.uuid4().hex
    tenant_id = f"tenant-rag-http-{run_id}"
    principal_id = f"principal-rag-http-{run_id}"
    motet_id = "motet-rag-http"
    conversation_id = f"conversation-rag-http-{run_id}"
    service_name = f"artifact_store_rag_http_{run_id}"

    raw_store = RedisArtifactStore(service_name=service_name)
    scoped_store = ScopedArtifactStore(
        raw_store,
        tenant_id=tenant_id,
        principal_id=principal_id,
        motet_id=motet_id,
    )
    embedding_service = create_embedding_service(topology="sibling")
    repository = ArtifactChunkRepository(embedding_dim=embedding_service.get_embedding_dimension())
    created_artifact_ids: list[str] = []
    captured_context_info: dict[str, Any] = {}

    cfg = SimpleNamespace(
        artifact_rag_enabled=True,
        artifact_rag_index_on_derivation=True,
        artifact_rag_top_k=3,
        artifact_rag_similarity_threshold=0.0,
        artifact_rag_chunk_size=3200,
        artifact_rag_chunk_overlap=400,
        artifact_rag_token_budget=1200,
        artifact_rag_hybrid_enabled=True,
        artifact_rag_vector_weight=0.7,
        artifact_rag_lexical_weight=0.3,
        artifact_rag_candidate_multiplier=4,
    )

    class _MotetStub:
        def __init__(self) -> None:
            self.tenant_id = tenant_id
            self.principal_id = principal_id
            self.motet_id = motet_id
            self.conversation_id = conversation_id
            self.task_id = f"task-{run_id}"
            self.command_id = f"command-{run_id}"
            self.stack = SimpleNamespace(config=cfg)
            self.artifact_store = scoped_store
            self._worker_context = {"embedding_service": embedding_service}

        def resolve_conversation_id(self, requested: str | None = None) -> str:
            return requested or self.conversation_id

        def log_fields(self, **extra: Any) -> dict[str, Any]:
            return {
                "tenant_id": self.tenant_id,
                "principal_id": self.principal_id,
                "motet_id": self.motet_id,
                "conversation_id": self.conversation_id,
                **extra,
            }

        def do(self, command: Any, data: Any) -> dict[str, Any]:
            assert command is rag_commands.rag_retrieve_context
            return _wrapped(rag_commands.rag_retrieve_context)(data)

        def dispatch(self, commands: list[Any]) -> list[str]:
            task_ids: list[str] = []
            for child in commands:
                command_type = child.get_command_type()
                if command_type.endswith("derive_upload_text"):
                    assert isinstance(child.data, DeriveUploadTextData)
                    result = _wrapped(derivation_commands.derive_upload_text)(child.data)
                    derived_id = str(result.get("id") or "")
                    if derived_id:
                        created_artifact_ids.append(derived_id)
                elif command_type.endswith("prepare_artifact_index"):
                    _wrapped(rag_commands.prepare_artifact_index)(child.data)
                task_ids.append(f"task-{command_type}-{len(task_ids)}")
            return task_ids

    motet = _MotetStub()

    def _execute_command(command: Any) -> dict[str, Any]:
        command_type = command.get_command_type()
        if not command_type.endswith("create_artifact"):
            raise AssertionError(f"Unexpected command from upload API: {command_type}")
        result = _wrapped(artifact_commands.create_artifact)(command.data)
        artifact_id = str(result.get("artifact_id") or "")
        if artifact_id:
            created_artifact_ids.append(artifact_id)
        return {"status": "completed", "result": {"status": "success", "data": result}}

    class _FakeMotetStack:
        def __init__(self, config: Any) -> None:
            self.config = config
            for key, value in vars(cfg).items():
                setattr(self.config, key, value)
            self.orchestrator = _OrchestratorStub()

        async def chat(self, messages: list[Message], context: dict[str, Any] | None = None) -> Response:
            core_messages = [
                Message(role=getattr(message, "role", "user"), content=getattr(message, "content", ""))
                for message in messages
            ]
            state = ContextPipelineState(messages=core_messages)
            prepared = RagContextProvider().apply(
                state,
                data=SimpleNamespace(
                    context=context or {},
                    analysis_metadata={
                        "rag": {
                            "needs_rag": True,
                            "artifact_intent": True,
                            "artifact_action": "question",
                            "suggested_scope": "conversation",
                            "confidence": 0.95,
                        }
                    },
                ),
                motet=motet,
                logger=_LoggerStub(),
            )
            captured_context_info.update(prepared.context_info)
            context_text = "\n".join(
                part.text
                for part in (prepared.messages[0].content_parts or [])
                if getattr(part, "type", None) == "text"
            )
            return Response(
                content=f"Prepared context:\n{context_text}",
                raw={"artifact_rag_citations": prepared.context_info.get("artifact_rag_citations", [])},
            )

    monkeypatch.setenv("MOTET_ALLOW_INSECURE_PRINCIPAL_HEADERS", "true")
    monkeypatch.setattr(artifact_commands, "get_motet_context", lambda: motet)
    monkeypatch.setattr(derivation_commands, "get_motet_context", lambda: motet)
    monkeypatch.setattr(rag_commands, "get_motet_context", lambda: motet)
    monkeypatch.setattr("motet.core.media.derivation_service.get_artifact_store", lambda: raw_store)
    monkeypatch.setattr("motet.core.workers.global_invoker.execute_command", _execute_command)
    monkeypatch.setattr("motet.core.MotetStack", _FakeMotetStack)

    app = create_app()
    headers = {
        "X-Principal-Id": principal_id,
        "X-Tenant-Id": tenant_id,
        "X-Motet-Id": motet_id,
    }
    source_id = ""
    try:
        with TestClient(app) as client:
            upload = client.post(
                "/api/v1/artifacts",
                headers=headers,
                params={"kind": "user_upload", "conversation_id": conversation_id},
                files={
                    "file": (
                        "orion-risk-register.txt",
                        (
                            "The Orion risk register says the mitigation is escrow rotation "
                            "every 30 days. Catering notes mention mint tea."
                        ).encode("utf-8"),
                        "text/plain",
                    )
                },
            )
            assert upload.status_code == 200, upload.text
            source_id = upload.json()["artifact_id"]

            chat = client.post(
                "/api/v1/chat",
                headers=headers,
                json={
                    "messages": [
                        {
                            "role": "user",
                            "content": "What mitigation does the Orion uploaded document require?",
                            "attachments": [
                                {
                                    "artifact_id": source_id,
                                    "filename": "orion-risk-register.txt",
                                    "content_type": "text/plain",
                                    "bytes": 120,
                                }
                            ],
                        }
                    ],
                    "conversation_id": conversation_id,
                    "stream": False,
                    "artifact_rag_scope": "conversation",
                },
            )
            assert chat.status_code == 200, chat.text

        chat_body = chat.json()
        body = chat_body["content"]
        assert captured_context_info["rag_context_enabled"] is True
        assert captured_context_info["artifact_rag_chunk_count"] >= 1
        assert chat_body["citations"][0]["source_label"] == "orion-risk-register.txt"
        assert chat_body["citations"][0]["artifact_id"] == source_id
        assert "<artifact_rag_context>" in body
        assert "Orion risk register" in body
        assert "escrow rotation every 30 days" in body
    finally:
        for artifact_id in reversed(created_artifact_ids):
            scoped_store.delete(artifact_id)
        if source_id:
            repository.delete_source_chunks(tenant_id=tenant_id, source_artifact_id=source_id)
        try:
            repository._redis.execute_command("FT.DROPINDEX", repository.index_name(tenant_id), "DD")
        except Exception:
            pass
