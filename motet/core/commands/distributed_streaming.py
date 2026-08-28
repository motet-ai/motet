"""
Motet - DistributedCommandStreamingMixin

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-03

Description:
    Task/command stream helper mixin for DistributedCommand (issue #158).

Usage:
    Mixed into DistributedCommand; not used standalone.

"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Optional

import structlog

from motet.core.commands.distributed_types import DistributedCommandContext

logger = structlog.get_logger(__name__)


class DistributedCommandStreamingMixin:
    """Mixin extracted from DistributedCommand (issue #158)."""

    # Host state initialized by DistributedCommand.__init__ / Command (for type checkers)
    command_id: str
    distributed_context: DistributedCommandContext
    stream_key: Optional[str]
    _stream_enabled: bool
    _stream_event_counter: int
    _stream_ttl: int

    if TYPE_CHECKING:
        def get_command_type(self) -> str: ...

    def _get_stream_key(self) -> str:
        """
        Get the Redis stream key for this command.
        
        Commands can override this to customize the stream key pattern.
        Default pattern: {command_type}_stream:{task_id}:events
        
        Returns:
            str: Redis stream key
        """
        if self.stream_key:
            return self.stream_key
        
        command_type = self.get_command_type()
        return f"{command_type}_stream:{self.distributed_context.task_id}:events"


    def _enable_streaming(self, stream_key: Optional[str] = None, ttl_seconds: int = 3600):
        """
        Enable streaming for this command.
        
        Args:
            stream_key: Optional custom stream key (uses default pattern if not provided)
            ttl_seconds: Time-to-live for stream in seconds (default: 1 hour) (ADR-0029)
        """
        self._stream_enabled = True
        self._stream_ttl = ttl_seconds  # Store for metadata (ADR-0029)
        if stream_key:
            self.stream_key = stream_key
        else:
            self.stream_key = self._get_stream_key()
        
        logger.debug(
            "command_streaming_enabled",
            command_id=self.command_id,
            command_type=self.get_command_type(),
            stream_key=self.stream_key,
        )


    def _stream_event(self, redis_client, event_type: str, **data):
        """
        Write an event to the Redis stream (if streaming is enabled).
        
        This is a sync method that should be called from within _do_execute (worker context).
        
        Args:
            redis_client: Synchronous Redis client instance
            event_type: Type of event (e.g., "token", "turn", "progress", "end", "error")
            **data: Additional event data to include in the stream
        """
        if not self._stream_enabled:
            return
        
        import time
        event_data = {
            "event": event_type,
            "timestamp": str(time.time()),
            "command_id": self.command_id,
            "command_type": self.get_command_type(),
            **data
        }
        
        try:
            redis_client.xadd(self.stream_key, event_data)
            self._stream_event_counter += 1  # Track events for metadata (ADR-0029)
            logger.debug(
                "command_stream_event_sent",
                command_id=self.command_id,
                command_type=self.get_command_type(),
                event_type=event_type,
                stream_key=self.stream_key,
            )
        except Exception as e:
            logger.warning(
                "command_stream_event_failed",
                command_id=self.command_id,
                command_type=self.get_command_type(),
                event_type=event_type,
                stream_key=self.stream_key,
                error=str(e),
                exc_info=True,
            )


    def _reset_stream(self, redis_client):
        """
        Reset (delete) the Redis stream (clears all existing data).
        
        WARNING: This deletes the stream and all existing events. Only use for
        command-specific streams, not unified task streams.
        
        Call this at the start of _do_execute for streaming commands that need
        a fresh stream.
        
        Args:
            redis_client: Synchronous Redis client instance
        """
        if not self._stream_enabled:
            return
        
        try:
            redis_client.delete(self.stream_key)
            logger.debug(
                "command_stream_reset",
                command_id=self.command_id,
                command_type=self.get_command_type(),
                stream_key=self.stream_key,
            )
        except Exception as e:
            logger.warning(
                "command_stream_reset_failed",
                command_id=self.command_id,
                command_type=self.get_command_type(),
                stream_key=self.stream_key,
                error=str(e),
                exc_info=True,
            )


    def _finalize_stream(self, redis_client, ttl_seconds: int = 3600):
        """
        Finalize the Redis stream (set expiration).
        
        Call this at the end of _do_execute for streaming commands.
        
        Args:
            redis_client: Synchronous Redis client instance
            ttl_seconds: Time-to-live in seconds (default: 1 hour)
        """
        if not self._stream_enabled:
            return
        
        try:
            redis_client.expire(self.stream_key, ttl_seconds)
            logger.debug(
                "command_stream_ttl_set",
                command_id=self.command_id,
                command_type=self.get_command_type(),
                stream_key=self.stream_key,
                ttl_seconds=ttl_seconds,
            )
        except Exception as e:
            logger.warning(
                "command_stream_ttl_set_failed",
                command_id=self.command_id,
                command_type=self.get_command_type(),
                stream_key=self.stream_key,
                ttl_seconds=ttl_seconds,
                error=str(e),
                exc_info=True,
            )


    def _cleanup_stream(self, redis_client):
        """
        Clean up the Redis stream (delete immediately).
        
        Call this in error handling or cleanup code.
        
        Args:
            redis_client: Synchronous Redis client instance
        """
        if not self._stream_enabled:
            return
        
        try:
            redis_client.delete(self.stream_key)
            logger.debug(
                "command_stream_cleanup",
                command_id=self.command_id,
                command_type=self.get_command_type(),
                stream_key=self.stream_key,
            )
        except Exception as e:
            logger.warning(
                "command_stream_cleanup_failed",
                command_id=self.command_id,
                command_type=self.get_command_type(),
                stream_key=self.stream_key,
                error=str(e),
                exc_info=True,
            )


    def _forward_stream_events(self, redis_client, source_stream_key: str, event_mapping: Optional[Dict[str, str]] = None):
        """
        Forward events from another stream to this command's stream.
        
        Useful for composing commands that delegate to other streaming commands.
        This is a blocking operation that reads until the source stream ends.
        
        Args:
            redis_client: Synchronous Redis client instance
            source_stream_key: The stream key to read events from
            event_mapping: Optional mapping of source event types to target event types
                         Example: {"token": "sub_token", "end": "sub_complete"}
        """
        if not self._stream_enabled:
            return
        
        event_mapping = event_mapping or {}
        last_id = "0"
        stream_ended = False
        
        logger.debug(
            "command_stream_forwarding_started",
            command_id=self.command_id,
            command_type=self.get_command_type(),
            source_stream_key=source_stream_key,
            destination_stream_key=self.stream_key,
        )
        
        while not stream_ended:
            try:
                # Read from source stream with timeout
                streams = redis_client.xread({source_stream_key: last_id}, count=10, block=1000)
                
                if not streams:
                    continue
                
                for stream_name, messages in streams:
                    for message_id, fields in messages:
                        last_id = message_id
                        
                        # Extract event type
                        event = fields.get('event', b'').decode('utf-8') if isinstance(fields.get('event'), bytes) else fields.get('event', '')
                        
                        # Map event type if needed
                        mapped_event = event_mapping.get(event, event)
                        
                        # Skip events mapped to None (allows filtering out specific events)
                        if mapped_event is None:
                            logger.debug(
                                "command_stream_forwarding_skipped",
                                command_id=self.command_id,
                                command_type=self.get_command_type(),
                                event=event,
                                source_stream_key=source_stream_key,
                                destination_stream_key=self.stream_key,
                            )
                            # Still stop if we hit end or error, even if skipped
                            if event in ["end", "error"]:
                                stream_ended = True
                                break
                            continue
                        
                        # Forward the event
                        forward_data = {}
                        for k, v in fields.items():
                            if k == 'event':
                                continue  # Skip event field, we'll use mapped_event
                            
                            # Decode bytes
                            key = k.decode('utf-8') if isinstance(k, bytes) else k
                            value = v.decode('utf-8') if isinstance(v, bytes) else v
                            forward_data[key] = value
                        
                        self._stream_event(redis_client, mapped_event, **forward_data)
                        
                        # Stop if we hit end or error
                        if event in ["end", "error"]:
                            stream_ended = True
                            break
                    
                    if stream_ended:
                        break
                        
            except Exception as e:
                logger.warning(
                    "command_stream_forwarding_failed",
                    command_id=self.command_id,
                    command_type=self.get_command_type(),
                    source_stream_key=source_stream_key,
                    destination_stream_key=self.stream_key,
                    error=str(e),
                    exc_info=True,
                )
                break
        
        logger.debug(
            "command_stream_forwarding_finished",
            command_id=self.command_id,
            command_type=self.get_command_type(),
            source_stream_key=source_stream_key,
            destination_stream_key=self.stream_key,
        )


    def _get_stream_event_count(self) -> int:
        """
        Get the number of events written to the stream.
        
        Returns:
            int: Number of events written during command execution
        """
        return self._stream_event_counter


