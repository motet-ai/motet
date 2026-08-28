"""
Motet - Schedule Storage

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Redis-based storage system for schedule metadata and execution tracking in the Motet
    distributed framework. Provides comprehensive schedule persistence, retrieval,
    and management with automatic datetime serialization and JSON handling.

Dependencies:
    - json: Data serialization and deserialization
    - uuid: Unique identifier generation
    - datetime: Time and date handling
    - structlog: Structured logging
    - typing: Type hints and annotations
    - Redis manager for distributed storage

Usage:
    from motet.core.orchestration.scheduling.storage import ScheduleStorage
    
    # Create storage
    storage = ScheduleStorage(redis_url="redis://localhost:6379/0")
    
    # Store schedule
    success = storage.store_schedule(schedule)
    
    # Retrieve schedule
    schedule = storage.get_schedule(schedule_id)

Notes:
    - Provides Redis-based schedule persistence and retrieval
    - Includes automatic datetime serialization and JSON handling
    - Supports comprehensive schedule metadata management
    - Includes schedule filtering and status tracking
    - Supports schedule execution tracking and results
    - Integrates with distributed Redis manager
    - Includes comprehensive error handling and logging
"""


import base64
import json
import os
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, cast

import structlog
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ...distributed.redis_manager import (
    store_structured_data_sync, retrieve_structured_data_sync
)
from ...security.encryption_service import (
    get_encryption_service,
    EncryptionError,
)
from .models import ScheduleMetadata, ScheduleFilter, ScheduleStatus

logger = structlog.get_logger(__name__)


class ScheduleStorage:
    """Redis-based storage for schedule metadata and execution tracking"""
    
    def __init__(self, redis_url: Optional[str] = None):
        self.redis_url = redis_url
        self.service_name = "schedule_storage"
        self._encryption_service = get_encryption_service()
    
    def _convert_datetime_to_iso(self, obj):
        """Recursively convert datetime objects to ISO format strings and sets to lists"""
        if isinstance(obj, datetime):
            logger.debug("Converting datetime to ISO: %s", obj)
            return obj.isoformat()
        elif isinstance(obj, set):
            logger.debug("Converting set to list: %s", obj)
            return list(obj)  # Convert sets to lists for JSON serialization
        elif isinstance(obj, dict):
            return {key: self._convert_datetime_to_iso(value) for key, value in obj.items()}
        elif isinstance(obj, list):
            return [self._convert_datetime_to_iso(item) for item in obj]
        else:
            return obj
    
    def _find_datetime_objects(self, obj, path=""):
        """Recursively find datetime objects and sets in data structure for debugging"""
        if isinstance(obj, datetime):
            logger.debug("Found datetime at path %s: %s", path, obj)
        elif isinstance(obj, set):
            logger.debug("Found set at path %s: %s", path, obj)
        elif isinstance(obj, dict):
            for key, value in obj.items():
                self._find_datetime_objects(value, f"{path}.{key}")
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                self._find_datetime_objects(item, f"{path}[{i}]")

    def store_schedule(self, schedule: ScheduleMetadata) -> bool:
        """Store a schedule in Redis"""
        try:
            key = f"schedules:active:{schedule.schedule_id}"
            data = self._serialize_schedule(schedule)
            
            store_structured_data_sync(
                self.service_name, key, data, format_type="hash"
            )
            
            # Add to type-specific sets for efficient querying
            self._add_to_type_sets(schedule)
            
            logger.info("Schedule stored successfully", 
                       schedule_id=schedule.schedule_id,
                       schedule_type=schedule.schedule_type)
            return True
            
        except Exception as e:
            logger.error("Failed to store schedule",
                        schedule_id=schedule.schedule_id,
                        error=str(e), exc_info=True)
            return False
    
    
    def retrieve_schedule(self, schedule_id: str) -> Optional[ScheduleMetadata]:
        """Retrieve a schedule from Redis"""
        try:
            key = f"schedules:active:{schedule_id}"
            data = retrieve_structured_data_sync(
                self.service_name, key, format_type="hash"
            )
            
            if not data:
                return None
            
            data = self._decrypt_sensitive_fields(data, schedule_id)
            # Convert ISO format strings back to datetime objects and handle enum conversions
            for field_name, field_value in data.items():
                # Handle None string values
                if isinstance(field_value, str) and field_value == 'None':
                    data[field_name] = None
                elif isinstance(field_value, str) and field_name in ['created_at', 'scheduled_at', 'recurring_until', 'last_execution_at', 'next_execution_at']:
                    try:
                        data[field_name] = datetime.fromisoformat(field_value)
                    except ValueError:
                        # If parsing fails, keep the string value
                        pass
                elif isinstance(field_value, str) and field_name == 'status':
                    # Convert enum string representation back to enum value
                    if field_value.startswith('ScheduleStatus.'):
                        enum_value = field_value.replace('ScheduleStatus.', '').lower()
                        data[field_name] = enum_value
                elif isinstance(field_value, str) and field_name == 'schedule_type':
                    # Convert enum string representation back to enum value
                    if field_value.startswith('ScheduleType.'):
                        enum_value = field_value.replace('ScheduleType.', '').lower()
                        data[field_name] = enum_value
                elif isinstance(field_value, str) and field_name in ['condition_check_interval', 'max_executions', 'execution_count', 'consecutive_failures', 'max_consecutive_failures']:
                    # Convert integer string values back to integers
                    try:
                        data[field_name] = int(field_value)
                    except ValueError:
                        # If parsing fails, keep the string value
                        pass
                elif isinstance(field_value, dict):
                    # Handle nested datetime objects in metadata
                    for nested_key, nested_value in field_value.items():
                        if isinstance(nested_value, str) and nested_value == 'None':
                            field_value[nested_key] = None
                        elif isinstance(nested_value, str) and nested_key in ['created_at', 'scheduled_at', 'recurring_until', 'last_execution_at', 'next_execution_at']:
                            try:
                                field_value[nested_key] = datetime.fromisoformat(nested_value)
                            except ValueError:
                                # If parsing fails, keep the string value
                                pass
            
            return ScheduleMetadata(**data)
            
        except Exception as e:
            logger.error("Failed to retrieve schedule",
                        schedule_id=schedule_id,
                        error=str(e), exc_info=True)
            return None
    
    
    def update_schedule(self, schedule: ScheduleMetadata) -> bool:
        """Update an existing schedule"""
        try:
            key = f"schedules:active:{schedule.schedule_id}"
            data = self._serialize_schedule(schedule)
            
            store_structured_data_sync(
                self.service_name, key, data, format_type="hash"
            )
            
            logger.info("Schedule updated successfully",
                       schedule_id=schedule.schedule_id)
            return True
            
        except Exception as e:
            logger.error("Failed to update schedule",
                        schedule_id=schedule.schedule_id,
                        error=str(e), exc_info=True)
            return False
    
    
    def delete_schedule(self, schedule_id: str) -> bool:
        """Delete a schedule from Redis"""
        try:
            # First retrieve to get type information for cleanup
            schedule = self.retrieve_schedule(schedule_id)
            if not schedule:
                return False
            
            key = f"schedules:active:{schedule_id}"
            
            # Delete the main schedule data
            from ...distributed.redis_manager import get_sync_redis_client
            redis_client = get_sync_redis_client()
            redis_client.delete(key)
            
            # Remove from type-specific sets
            self._remove_from_type_sets(schedule)
            
            logger.info("Schedule deleted successfully",
                       schedule_id=schedule_id)
            return True
            
        except Exception as e:
            logger.error("Failed to delete schedule",
                        schedule_id=schedule_id,
                        error=str(e), exc_info=True)
            return False
    
    def list_schedules(self, filters: Optional[ScheduleFilter] = None) -> List[ScheduleMetadata]:
        """List schedules with optional filtering"""
        try:
            if not filters:
                filters = ScheduleFilter()
            
            # Optimize: Use Redis set intersections when we have tenant/motet/principal filters
            # This avoids retrieving all schedules and filtering in-memory
            schedule_ids = self._get_schedule_ids_optimized(filters)
            
            schedules = []
            for schedule_id in schedule_ids:
                schedule = self.retrieve_schedule(schedule_id)
                if schedule and self._matches_filters(schedule, filters):
                    schedules.append(schedule)
            
            # Apply sorting and pagination
            schedules.sort(key=lambda s: s.created_at, reverse=True)
            
            start = filters.offset
            end = start + filters.limit
            return schedules[start:end]
            
        except Exception as e:
            logger.error("Failed to list schedules",
                        error=str(e), exc_info=True)
            return []
    
    def _add_to_type_sets(self, schedule: ScheduleMetadata) -> None:
        """Add schedule to type-specific sets for efficient querying"""
        from ...distributed.redis_manager import get_sync_redis_client
        redis_client = get_sync_redis_client()
        
        # Debug: Check schedule_type
        logger.debug(f"Adding to type sets - schedule_type: {schedule.schedule_type}, type: {type(schedule.schedule_type)}")
        
        # Add to general active schedules set
        redis_client.sadd("schedules:active", schedule.schedule_id)
        
        # Add to type-specific sets
        # Handle both enum and string types
        if hasattr(schedule.schedule_type, 'value'):
            type_value = schedule.schedule_type.value
        else:
            type_value = str(schedule.schedule_type)
        
        type_key = f"schedules:type:{type_value}"
        redis_client.sadd(type_key, schedule.schedule_id)
        
        # Add to tenant-specific set if tenant_id is provided
        if schedule.tenant_id:
            tenant_key = f"schedules:tenant:{schedule.tenant_id}"
            redis_client.sadd(tenant_key, schedule.schedule_id)
        
        # Add to motet-specific set if motet_id is provided (ADR-0056)
        if schedule.motet_id:
            motet_key = f"schedules:motet:{schedule.motet_id}"
            redis_client.sadd(motet_key, schedule.schedule_id)
        
        # Add to principal-specific set if created_by (principal_id) is provided
        # This enables efficient querying of schedules by principal
        if schedule.created_by:
            principal_key = f"schedules:principal:{schedule.created_by}"
            redis_client.sadd(principal_key, schedule.schedule_id)
    
    
    def _remove_from_type_sets(self, schedule: ScheduleMetadata) -> None:
        """Remove schedule from type-specific sets"""
        from ...distributed.redis_manager import get_sync_redis_client
        redis_client = get_sync_redis_client()
        
        # Remove from general active schedules set
        redis_client.srem("schedules:active", schedule.schedule_id)
        
        # Remove from type-specific sets
        type_key = f"schedules:type:{schedule.schedule_type.value}"
        redis_client.srem(type_key, schedule.schedule_id)
        
        # Remove from tenant-specific set if tenant_id is provided
        if schedule.tenant_id:
            tenant_key = f"schedules:tenant:{schedule.tenant_id}"
            redis_client.srem(tenant_key, schedule.schedule_id)
        
        # Remove from motet-specific set if motet_id is provided (ADR-0056)
        if schedule.motet_id:
            motet_key = f"schedules:motet:{schedule.motet_id}"
            redis_client.srem(motet_key, schedule.schedule_id)
        
        # Remove from principal-specific set if created_by (principal_id) is provided
        if schedule.created_by:
            principal_key = f"schedules:principal:{schedule.created_by}"
            redis_client.srem(principal_key, schedule.schedule_id)
    
    def _get_schedule_ids_by_type(self, schedule_type: Optional[str] = None) -> List[str]:
        """Get schedule IDs filtered by type"""
        from ...distributed.redis_manager import get_sync_redis_client
        redis_client = get_sync_redis_client()
        
        if schedule_type:
            type_key = f"schedules:type:{schedule_type}"
            return list(cast(Any, redis_client.smembers(type_key)))
        else:
            return list(cast(Any, redis_client.smembers("schedules:active")))
    
    def _get_schedule_ids_optimized(self, filters: ScheduleFilter) -> List[str]:
        """
        Get schedule IDs using Redis set intersections for efficient filtering.
        
        Uses set intersections (SINTER) when we have tenant/motet/principal filters
        to avoid retrieving all schedules and filtering in-memory.
        
        Performance: O(N*M) where N is smallest set size, M is number of sets
        vs O(N) for retrieving all schedules and filtering in-memory.
        """
        from ...distributed.redis_manager import get_sync_redis_client
        redis_client = get_sync_redis_client()
        
        # Start with type-based set (or all active schedules)
        sets_to_intersect = []
        
        if filters.schedule_type:
            type_key = f"schedules:type:{filters.schedule_type}"
            sets_to_intersect.append(type_key)
        else:
            sets_to_intersect.append("schedules:active")
        
        # Add tenant filter if provided
        if filters.tenant_id:
            tenant_key = f"schedules:tenant:{filters.tenant_id}"
            sets_to_intersect.append(tenant_key)
        
        # Add motet filter if provided
        if filters.motet_id:
            motet_key = f"schedules:motet:{filters.motet_id}"
            sets_to_intersect.append(motet_key)
        
        # Add principal filter if provided
        if filters.created_by:
            principal_key = f"schedules:principal:{filters.created_by}"
            sets_to_intersect.append(principal_key)
        
        # If we have multiple sets, use intersection for efficient filtering
        if len(sets_to_intersect) > 1:
            # Redis SINTER returns the intersection of all sets
            result = cast(Any, redis_client.sinter(sets_to_intersect))
            schedule_ids = list(result) if result else []
            logger.debug("schedule_query_optimized_intersection",
                        sets_intersected=sets_to_intersect,
                        result_count=len(schedule_ids))
            return schedule_ids
        elif len(sets_to_intersect) == 1:
            # Single set - just get all members
            schedule_ids = list(cast(Any, redis_client.smembers(sets_to_intersect[0])))
            logger.debug("schedule_query_single_set",
                        set_key=sets_to_intersect[0],
                        result_count=len(schedule_ids))
            return schedule_ids
        else:
            # No filters - fall back to all active schedules
            schedule_ids = list(cast(Any, redis_client.smembers("schedules:active")))
            logger.debug("schedule_query_all_active",
                        result_count=len(schedule_ids))
            return schedule_ids
    
    def _matches_filters(self, schedule: ScheduleMetadata, filters: ScheduleFilter) -> bool:
        """Check if a schedule matches the given filters"""
        if filters.status and schedule.status != filters.status:
            return False
        if filters.tenant_id and schedule.tenant_id != filters.tenant_id:
            return False
        if filters.motet_id and schedule.motet_id != filters.motet_id:
            return False
        if filters.created_by and schedule.created_by != filters.created_by:
            return False
        if filters.created_after and schedule.created_at < filters.created_after:
            return False
        if filters.created_before and schedule.created_at > filters.created_before:
            return False
        if filters.next_execution_after and schedule.next_execution_at and schedule.next_execution_at < filters.next_execution_after:
            return False
        if filters.next_execution_before and schedule.next_execution_at and schedule.next_execution_at > filters.next_execution_before:
            return False
        return True

    def _serialize_schedule(self, schedule: ScheduleMetadata) -> Dict[str, Any]:
        """Convert schedule to a dict with encrypted sensitive fields."""
        raw = schedule.model_dump()
        converted = self._convert_datetime_to_iso(raw)
        if not isinstance(converted, dict):
            raise TypeError(
                f"Schedule serialization expected dict after datetime conversion, got {type(converted).__name__}"
            )
        return self._encrypt_sensitive_fields(converted, schedule.schedule_id)

    def _encrypt_sensitive_fields(self, data: Dict[str, Any], schedule_id: str) -> Dict[str, Any]:
        """Encrypt metadata/condition/error fields using envelope encryption."""
        tenant_id = data.get("tenant_id")
        if not tenant_id:
            raise ValueError(
                f"Schedule {schedule_id} is missing tenant_id; cannot encrypt sensitive fields"
            )
        
        sensitive_payload = {
            "metadata": data.get("metadata") or {},
            "condition_expression": data.get("condition_expression"),
            "last_error": data.get("last_error"),
        }
        
        payload_bytes = json.dumps(sensitive_payload).encode("utf-8")
        encryption_start = time.time()
        dek = AESGCM.generate_key(bit_length=256)
        aesgcm = AESGCM(dek)
        iv = os.urandom(12)
        encrypted_bytes = aesgcm.encrypt(iv, payload_bytes, None)
        encryption_time_ms = round((time.time() - encryption_start) * 1000, 2)
        
        wrap_start = time.time()
        wrapped_dek = self._encryption_service.wrap_key(dek, tenant_id)
        dek_wrap_time_ms = round((time.time() - wrap_start) * 1000, 2)
        
        data["_sensitive_envelope"] = {
            "encrypted": True,
            "encryption_mode": "envelope-v1",
            "encryption": {
                "encrypted_data": base64.b64encode(encrypted_bytes).decode("utf-8"),
                "iv": base64.b64encode(iv).decode("utf-8"),
                "tenant_id": tenant_id,
                "encryption_version": "aes-256-gcm-v1",
                "encryption_time_ms": encryption_time_ms,
                "dek_wrap_time_ms": dek_wrap_time_ms,
            },
            "dek": wrapped_dek,
        }
        
        data["metadata"] = None
        data["condition_expression"] = None
        data["last_error"] = None
        return data

    def _decrypt_sensitive_fields(self, data: Dict[str, Any], schedule_id: str) -> Dict[str, Any]:
        """Decrypt sensitive payload if envelope data is present."""
        envelope = data.pop("_sensitive_envelope", None)
        if not envelope:
            # Legacy/plaintext schedules (not expected but handled defensively)
            data["metadata"] = data.get("metadata") or {}
            return data
        
        encrypted_blob = envelope.get("encryption")
        wrapped_dek = envelope.get("dek")
        if not encrypted_blob or not wrapped_dek:
            raise ValueError(f"Schedule {schedule_id} has invalid sensitive envelope")
        
        try:
            dek = self._encryption_service.unwrap_key(wrapped_dek)
            aesgcm = AESGCM(dek)
            iv = base64.b64decode(encrypted_blob["iv"])
            encrypted_bytes = base64.b64decode(encrypted_blob["encrypted_data"])
            payload_bytes = aesgcm.decrypt(iv, encrypted_bytes, None)
            sensitive_payload = json.loads(payload_bytes.decode("utf-8"))
        except (EncryptionError, ValueError, KeyError) as exc:
            logger.error(
                "Failed to decrypt schedule metadata",
                schedule_id=schedule_id,
                error=str(exc),
                exc_info=True,
            )
            raise ValueError(f"Unable to decrypt schedule {schedule_id}: {exc}") from exc
        
        data["metadata"] = sensitive_payload.get("metadata") or {}
        data["condition_expression"] = sensitive_payload.get("condition_expression")
        data["last_error"] = sensitive_payload.get("last_error")
        return data
