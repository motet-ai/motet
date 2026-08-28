"""
Motet - Events API

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Events API for the Motet distributed framework.
    Provides REST API endpoints for real-time event streaming and event statistics.

Dependencies:
    - fastapi: Web framework for REST API
    - fastapi.responses: StreamingResponse for SSE
    - motet.core.distributed: Redis managers and event bus

Usage:
    from motet.interfaces.api.v1.events import router
    
    # Include in FastAPI app
    app.include_router(router)

Notes:
    - Provides real-time SSE stream of tenant EventBus events from Redis pub/sub
    - Subscribes to ``{tenant_id}:events:channel`` only (issue #233)
    - Fail-closed: events with missing or mismatched tenant_id are dropped
    - Optional result unpacking for easier frontend consumption
    - Part of Phase 2: API Organization and URL Standardization
"""

from typing import Dict, Any, AsyncGenerator, Optional
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
import structlog
import json
import asyncio

from ..shared.auth import get_current_principal
from ..shared.identity import get_principal_context
from ....core.distributed.tenant_keys import event_bus_channel
from ....core.types import Principal

logger = structlog.get_logger(__name__)


def event_matches_caller_tenant(event: Dict[str, Any], caller_tenant: str) -> bool:
    """
    Fail-closed tenant check for SSE (issue #233).

    Returns True only when ``data.tenant_id`` is present and equals the
    authenticated caller tenant. Missing or mismatched tenant_id is dropped.
    """
    event_data = event.get("data") if isinstance(event.get("data"), dict) else {}
    event_tenant_id = str(event_data.get("tenant_id") or "").strip()
    return bool(event_tenant_id) and event_tenant_id == caller_tenant


router = APIRouter(prefix="/api/v1/events", tags=["events"])


class EventStatsResponse(BaseModel):
    """Response model for event statistics."""
    published: int = Field(..., description="Total events published", json_schema_extra={"example": 1234})
    failures: int = Field(..., description="Total event publishing failures", json_schema_extra={"example": 5})


@router.get(
    "",
    summary="Stream real-time events",
    description="Get a real-time SSE stream of EventBus events for the caller tenant",
    response_description="SSE stream of events"
)
async def stream_events(
    request: Request,
    unpack_result: bool = Query(
        False,
        description="If true, unpack result.data from command completion events into event.data for easier frontend consumption"
    ),
    event_kinds: Optional[str] = Query(
        None,
        description="Comma-separated list of event kinds to filter (e.g., 'create_artifact_completed,derive_upload_image_completed'). If not provided, all events are streamed (subject to tenant/principal filtering)."
    ),
    principal: Principal = Depends(get_current_principal)
) -> StreamingResponse:
    """
    Stream real-time events from the event bus.
    
    Subscribes to the caller tenant EventBus channel and streams events as SSE.
    
    **Security:**
    - Subscribes only to ``{tenant_id}:events:channel`` (issue #233)
    - Events with missing or mismatched tenant_id are dropped (fail-closed)
    - Further filtered by principal_id and motet_id when those fields are present
    - Authentication required via JWT, service account, or API key
    
    **Features:**
    - `unpack_result=true`: Extracts result.data from command completion events and merges into event.data
      This makes fields like `source_artifact_id` and `artifact_id` directly accessible without parsing nested structures.
    - `event_kinds`: Filter to specific event types (comma-separated) to reduce bandwidth
    
    Args:
        unpack_result: If True, extracts result.data from command completion events and merges into event.data
        event_kinds: Optional comma-separated list of event kinds to filter (e.g., 'create_artifact_completed')
        principal: Authenticated principal (from JWT, service account, or headers)
        
    Returns:
        StreamingResponse with text/event-stream media type
        
    Example:
        ```bash
        # Basic stream (all events for current tenant/principal)
        curl -N -H "Authorization: Bearer <token>" http://localhost:8000/api/v1/events
        
        # With result unpacking for easier frontend consumption
        curl -N -H "Authorization: Bearer <token>" "http://localhost:8000/api/v1/events?unpack_result=true"
        
        # Filter to specific event types
        curl -N -H "Authorization: Bearer <token>" "http://localhost:8000/api/v1/events?event_kinds=create_artifact_completed,derive_upload_image_completed"
        ```
    """
    # Parse event kinds filter
    allowed_kinds = None
    if event_kinds:
        allowed_kinds = {kind.strip() for kind in event_kinds.split(",") if kind.strip()}
    motet_id, tenant_id, principal_id = get_principal_context(principal)
    
    async def event_stream() -> AsyncGenerator[bytes, None]:
        """Generate SSE stream for event bus events with security filtering."""
        from ....core.distributed.redis_manager import get_pubsub_redis_client
        
        redis_client = get_pubsub_redis_client("events_sse")
        pubsub = None
        
        caller_channel = event_bus_channel(tenant_id)
        try:
            # Create pub/sub connection on the dedicated pub/sub pool
            pubsub = redis_client.pubsub()
            await pubsub.subscribe(caller_channel)
            
            while True:
                if await request.is_disconnected():
                    logger.debug(
                        "Event stream client disconnected",
                        tenant_id=tenant_id,
                        principal_id=principal_id,
                        motet_id=motet_id,
                    )
                    break

                # Listen for messages from pub/sub channel with timeout
                message = await pubsub.get_message(timeout=1.0)
                if message and message['type'] == 'message':
                    event_json = message['data']
                    event = json.loads(event_json)
                    
                    # SECURITY: fail-closed tenant isolation (issue #233).
                    event_data = event.get("data", {}) if isinstance(event.get("data"), dict) else {}
                    event_principal_id = event_data.get("principal_id", "")
                    event_motet_id = event_data.get("motet_id", "")
                    
                    if not event_matches_caller_tenant(event, tenant_id):
                        continue
                    
                    # Skip events that don't match the authenticated principal (if principal_id is present in event)
                    # Note: Some system events may not have principal_id, which is fine
                    if event_principal_id and event_principal_id != principal_id:
                        continue
                    
                    # Skip events that don't match the authenticated principal's motet (if motet_id is present in event)
                    # Note: Some system events may not have motet_id, which is fine
                    if event_motet_id and event_motet_id != motet_id:
                        continue
                    
                    # Filter by event kind if specified
                    if allowed_kinds and event.get("kind") not in allowed_kinds:
                        continue
                    
                    # Unpack result.data if requested
                    if unpack_result and event.get("kind", "").endswith("_completed"):
                        result = event_data.get("result", {})
                        
                        # Handle Redis-stored results: retrieve from Redis if needed
                        if isinstance(result, dict) and "_redis_result_key" in result:
                            try:
                                from ....core.distributed.redis_command_data_manager import RedisCommandDataManager
                                redis_manager = RedisCommandDataManager()
                                redis_key = result["_redis_result_key"]
                                # Retrieve the full result from Redis
                                retrieved_result = redis_manager.retrieve_command_result(
                                    redis_key,
                                    tenant_id=tenant_id,
                                    motet_id=motet_id
                                )
                                # Replace the Redis pointer with the actual result
                                result = retrieved_result
                                logger.debug(
                                    "Retrieved result from Redis for event unpacking",
                                    redis_key=redis_key,
                                    event_kind=event.get("kind"),
                                    result_status=retrieved_result.get("status") if isinstance(retrieved_result, dict) else None,
                                    result_has_data=isinstance(retrieved_result, dict) and "data" in retrieved_result
                                )
                            except Exception as e:
                                logger.warning(
                                    "Failed to retrieve result from Redis for event unpacking",
                                    redis_key=result.get("_redis_result_key"),
                                    event_kind=event.get("kind"),
                                    error=str(e),
                                    exc_info=True
                                )
                                # Continue with original result (Redis pointer) if retrieval fails
                        
                        # Extract result.data if it exists (ADR-0029 format)
                        if isinstance(result, dict) and result.get("status") == "success":
                            result_data = result.get("data", {})
                            if isinstance(result_data, dict) and result_data:
                                # Standard ADR-0029: result has nested data field
                                # Merge result.data fields into event.data for easier access
                                # Preserve original result field for backward compatibility
                                event_data = {**event_data, **result_data}
                                event["data"] = event_data
                                logger.debug(
                                    "Unpacked result.data into event.data",
                                    event_kind=event.get("kind"),
                                    unpacked_keys=list(result_data.keys())[:10],  # Log first 10 keys
                                    has_artifact_id="artifact_id" in result_data,
                                    has_source_artifact_id="source_artifact_id" in result_data
                                )
                            elif isinstance(result_data, dict) and not result_data:
                                # Result is ADR-0029 formatted but data is empty or missing
                                # Check if important fields are at top level of result (e.g., source_artifact_id)
                                # This handles cases where commands return flat ADR-0029 format
                                top_level_fields = {}
                                for key in ["source_artifact_id", "artifact_id", "total_pages", "page_num"]:
                                    if key in result:
                                        top_level_fields[key] = result[key]
                                if top_level_fields:
                                    event_data = {**event_data, **top_level_fields}
                                    event["data"] = event_data
                                    logger.debug(
                                        "Unpacked top-level result fields into event.data",
                                        event_kind=event.get("kind"),
                                        unpacked_keys=list(top_level_fields.keys()),
                                        has_source_artifact_id="source_artifact_id" in top_level_fields
                                    )
                    
                    # Send with event name for proper SSE format
                    yield f"event: event_bus\ndata: {json.dumps(event)}\n\n".encode("utf-8")
        except asyncio.CancelledError:
            logger.debug(
                "Event stream cancelled",
                tenant_id=tenant_id,
                principal_id=principal_id,
                motet_id=motet_id,
            )
            raise
        except Exception as e:
            logger.error(
                "Event stream error",
                error=str(e),
                tenant_id=tenant_id,
                principal_id=principal_id,
                motet_id=motet_id,
                exc_info=True
            )
            yield f"data: {json.dumps({'kind': 'error', 'message': str(e)})}\n\n".encode("utf-8")
        finally:
            # Clean up pub/sub connection
            if pubsub:
                try:
                    await pubsub.unsubscribe(caller_channel)
                    await pubsub.close()
                except Exception:
                    pass  # Ignore cleanup errors
    
    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@router.get(
    "/stats",
    summary="Get event statistics",
    description="Get statistics about event bus publishing (total published, failures)",
    response_model=EventStatsResponse,
    response_description="Event bus statistics"
)
async def get_event_stats(
    principal: Principal = Depends(get_current_principal)
) -> EventStatsResponse:
    """
    Get event bus statistics.
    
    Returns statistics about the distributed event bus including:
    - Total events published
    - Total publishing failures
    
    Useful for monitoring event bus health and throughput.
    
    Args:
        principal: Authenticated principal (from JWT, service account, or headers)
        
    Returns:
        EventStatsResponse with published and failure counts
    """
    try:
        from ....core.workers import global_bus as _bus
        return EventStatsResponse(
            published=_bus.published_count,
            failures=_bus.failure_count
        )
    except Exception as e:
        logger.warning("Failed to get event stats", error=str(e))
        return EventStatsResponse(published=0, failures=0)

