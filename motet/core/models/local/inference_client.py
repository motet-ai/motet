"""
Motet - Local Inference Client

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Lightweight local inference client for Celery workers. Uses Redis Streams
    for request/response communication with local inference process pool. Pure I/O
    operation that works with ANY worker pool type (fork, threads, eventlet, gevent).
    Supports CPU, NVIDIA GPU, and Apple Silicon inference.

    Follows the same pattern as MotetMCPClient. Per the sibling
    LocalInferenceManager is hoisted out of the worker lifecycle; the client routes on
    a shared ``manager_id`` (MOTET_LOCAL_INFERENCE_MANAGER_ID) rather than per-worker
    ``worker_id`` so one long-lived manager serves all workers in an orchestrated unit.

Dependencies:
    - redis: Redis Streams for communication
    - structlog: Structured logging

Usage:
    from motet.core.models.local.inference_client import LocalInferenceClient
    from motet.core.distributed.redis_manager import UnifiedRedisManager
    
    redis_client = UnifiedRedisManager.get_sync_client()
    client = LocalInferenceClient(redis_client)
    
    response = client.infer(
        model_id="meta-llama/Llama-2-7b-chat-hf",
        messages=[{"role": "user", "content": "Hello!"}],
        temperature=0.7,
        max_tokens=1000
    )

Notes:
    - Pure I/O operation - no device-specific code in workers
    - Works with ANY Celery pool type (fork, threads, eventlet, gevent)
    - Automatically batched by LocalInferenceManager
    - Follows MCP parent process pattern
"""

import os
import json
import time
from uuid import uuid4
from typing import Dict, Any, List, Optional
import structlog

logger = structlog.get_logger(__name__)


class LocalInferenceClient:
    """
    Lightweight client for local inference from Celery workers.
    
    This client makes local inference a pure I/O operation that works on
    ANY worker pool type. The actual inference processing happens in a separate
    process pool managed by LocalInferenceManager (like MCPInstanceManager).
    Supports CPU, NVIDIA GPU, and Apple Silicon.
    """
    
    def __init__(self, redis_client):
        """
        Initialize local inference client.
        
        Args:
            redis_client: Redis client instance (sync or async)
        """
        self.redis = redis_client
        # worker_id is observability-only: it rides in the request body so the manager
        # can attribute a call to its originating worker. It is NO LONGER part of the
        # bus address (ADR-0105 §R2).
        self.worker_id = os.getenv('CELERY_WORKER_ID', 'unknown')
        # manager_id is the Redis Streams routing prefix shared with the sibling
        # LocalInferenceManager (ADR-0105 §R2/§R3). All workers in an orchestrated unit
        # publish to the same manager_id-keyed streams so one hoisted manager serves them
        # all, independent of any single worker's lifecycle.
        self.manager_id = os.getenv('MOTET_LOCAL_INFERENCE_MANAGER_ID', 'local-inference-default')
        logger.info(
            "Initialized local inference client",
            worker_id=self.worker_id,
            manager_id=self.manager_id,
        )
    
    def infer(
        self,
        model_id: str,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 1000,
        timeout: float = 30.0,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Send inference request to local inference pool and wait for response.
        
        Pure I/O operation:
        1. Publish request to Redis Stream
        2. Wait for response on dedicated stream
        3. Return result
        
        Works on ANY worker pool type (fork, threads, eventlet, gevent).
        
        Args:
            model_id: Model identifier (HuggingFace model ID or local path)
            messages: List of message dicts with 'role' and 'content'
            temperature: Sampling temperature (0.0-2.0)
            max_tokens: Maximum tokens to generate
            timeout: Request timeout in seconds
            **kwargs: Additional inference parameters (quantization, etc.)
        
        Returns:
            Dictionary with inference result:
            - success: bool
            - result: Dict with 'text' and optional 'finish_reason'
            - model_id: str
            - elapsed_seconds: float
            - request_id: str
        
        Raises:
            TimeoutError: If inference times out
            RuntimeError: If inference fails
        """
        request_id = str(uuid4())
        
        # Prepare request
        request = {
            'request_id': request_id,
            'model_id': model_id,
            'messages': messages,
            'temperature': temperature,
            'max_tokens': max_tokens,
            'worker_id': self.worker_id,
            'timestamp': time.time(),
            **kwargs  # Include additional parameters
        }
        
        logger.info(
            "Sending local inference request",
            request_id=request_id,
            model_id=model_id,
            message_count=len(messages),
            max_tokens=max_tokens
        )
        
        try:
            # Publish request to the manager-keyed local inference stream (ADR-0105 §R2)
            request_stream = f'local-inference:{self.manager_id}:requests'
            
            logger.info("Publishing request to local inference stream",
                       request_id=request_id,
                       request_stream=request_stream,
                       worker_id=self.worker_id,
                       manager_id=self.manager_id,
                       model_id=model_id)
            
            self.redis.xadd(
                request_stream,
                {'data': json.dumps(request)}
            )
            
            logger.info("Request published, waiting for response",
                       request_id=request_id,
                       timeout=timeout)
            
            # Wait for response (pure I/O - cooperatively yields)
            start_time = time.time()
            response_stream = f'local-inference:{self.manager_id}:responses:{request_id}'
            
            while time.time() - start_time < timeout:
                # Non-blocking read with 100ms timeout (yields to event loop)
                result = self.redis.xread(
                    {response_stream: '0'},
                    count=1,
                    block=100  # 100ms - cooperatively yields
                )
                
                if result and len(result) > 0:
                    # Extract response data
                    stream_data = result[0][1][0]  # [stream_name, [(id, data)]]
                    response_bytes = stream_data[1].get(b'data') or stream_data[1].get('data')
                    
                    if isinstance(response_bytes, bytes):
                        response_data = json.loads(response_bytes.decode('utf-8'))
                    else:
                        response_data = json.loads(response_bytes)
                    
                    # Cleanup response stream
                    try:
                        self.redis.delete(response_stream)
                    except Exception as e:
                        logger.warning("Failed to cleanup response stream", error=str(e))
                    
                    elapsed = time.time() - start_time
                    
                    # Check if request was successful
                    if not response_data.get('success', False):
                        error_msg = response_data.get('error', 'Unknown error')
                        logger.error(
                            "Local inference failed",
                            request_id=request_id,
                            error=error_msg,
                            elapsed_seconds=round(elapsed, 3)
                        )
                        raise RuntimeError(f"Local inference failed: {error_msg}")
                    
                    logger.info(
                        "Local inference completed",
                        request_id=request_id,
                        elapsed_seconds=round(elapsed, 3)
                    )
                    
                    return response_data
            
            # Timeout
            elapsed = time.time() - start_time
            logger.error(
                "Local inference timeout",
                request_id=request_id,
                timeout=timeout,
                elapsed=elapsed
            )
            
            # Cleanup on timeout
            try:
                self.redis.delete(response_stream)
            except Exception:
                pass  # best-effort cleanup; proceed with timeout error
            
            raise TimeoutError(f"Local inference timeout after {timeout}s")
            
        except (TimeoutError, RuntimeError):
            # Re-raise expected errors
            raise
        except Exception as e:
            logger.error(
                "Local inference request failed",
                request_id=request_id,
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True
            )
            raise RuntimeError(f"Local inference request failed: {e}") from e
    
    def infer_sync(self, *args, **kwargs) -> Dict[str, Any]:
        """
        Synchronous version of infer() for non-async contexts.
        Simply calls infer() since Redis operations are already sync.
        
        This is an alias for infer() to maintain API consistency.
        """
        return self.infer(*args, **kwargs)
    
    def infer_stream(
        self,
        model: str,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 8000,
        timeout: float = 120.0,
        **kwargs
    ):
        """
        Send streaming inference request and yield tokens as they arrive.
        
        Pure I/O operation that works on ANY worker pool type.
        Yields text tokens incrementally via Redis Streams.
        
        Args:
            model: Model identifier
            messages: Conversation messages
            temperature: Sampling temperature (0.0-2.0)
            max_tokens: Maximum tokens to generate
            timeout: Maximum *idle* seconds between stream events. The timer
                resets every time an event arrives, so a slow-but-progressing
                local generation is never killed mid-stream; only a stalled
                one is. (Previously this was a total wall-clock cap, which
                discarded entire turns that streamed fine but ran long.)
            **kwargs: Extra request fields forwarded verbatim to the manager
                (e.g. ``json_schema`` / ``gbnf_grammar`` for ADR-0114
                grammar-constrained decoding).
            
        Yields:
            str: Text tokens as they are generated
            
        Raises:
            TimeoutError: If inference exceeds timeout
            RuntimeError: If inference fails
            
        Example:
            for token in client.infer_stream(model="phi-4-mini", messages=messages):
                print(token, end='', flush=True)
        """
        request_id = str(uuid4())
        
        # Prepare request with stream flag
        request = {
            'request_id': request_id,
            'model': model,
            'messages': messages,
            'temperature': temperature,
            'max_tokens': max_tokens,
            'worker_id': self.worker_id,
            'timestamp': time.time(),
            'stream': True,  # Enable streaming
            # Forward extra params (e.g. json_schema / gbnf_grammar for
            # ADR-0114 constrained decoding) so the manager can compile a
            # grammar for the streaming path as well as the unary path.
            **kwargs,
        }
        
        logger.info(
            "Sending streaming local inference request",
            request_id=request_id,
            model=model,
            message_count=len(messages)
        )
        
        # Publish request to the manager-keyed stream (ADR-0105 §R2)
        request_stream = f'local-inference:{self.manager_id}:requests'
        self.redis.xadd(
            request_stream,
            {'data': json.dumps(request)}
        )
        
        # Stream tokens as they arrive
        start_time = time.time()
        response_stream = f'local-inference:{self.manager_id}:responses:{request_id}'
        last_id = '0'  # Start from beginning
        # Idle-based deadline: reset on every received event so progressing
        # generations are never killed; only a stalled stream times out.
        last_event_time = start_time
        
        try:
            while time.time() - last_event_time < timeout:
                # Non-blocking read with 100ms timeout (yields to event loop)
                result = self.redis.xread(
                    {response_stream: last_id},
                    count=1,
                    block=100  # 100ms - cooperatively yields
                )
                
                if result and len(result) > 0:
                    # Extract response data
                    stream_name, messages_list = result[0]
                    message_id, message_data = messages_list[0]
                    
                    # Update last_id for next read
                    last_id = message_id
                    last_event_time = time.time()
                    
                    response_bytes = message_data.get(b'data') or message_data.get('data')
                    
                    if isinstance(response_bytes, bytes):
                        response_data = json.loads(response_bytes.decode('utf-8'))
                    else:
                        response_data = json.loads(response_bytes)
                    
                    # Check for errors
                    if not response_data.get('success', True):
                        error_msg = response_data.get('error', 'Unknown error')
                        logger.error(
                            "Streaming local inference failed",
                            request_id=request_id,
                            error=error_msg
                        )
                        raise RuntimeError(f"Local inference failed: {error_msg}")
                    
                    # Check for completion marker
                    if response_data.get('done', False):
                        elapsed = time.time() - start_time
                        logger.info(
                            "Streaming local inference completed",
                            request_id=request_id,
                            elapsed_seconds=round(elapsed, 3)
                        )

                        # Surface terminal usage/finish_reason to the adapter as a
                        # typed 'final' event before completing the stream.
                        finish_reason = response_data.get('finish_reason')
                        usage = response_data.get('usage')
                        if finish_reason is not None or usage is not None:
                            yield {
                                'type': 'final',
                                'finish_reason': finish_reason,
                                'usage': usage,
                            }

                        # Cleanup response stream
                        try:
                            self.redis.delete(response_stream)
                        except Exception as e:
                            logger.warning("Failed to cleanup response stream", error=str(e))
                        
                        return  # Stop iteration

                    # Forward typed events (text / thinking / tool_call_*) verbatim
                    # so the adapter can translate them to canonical stream events.
                    if response_data.get('type'):
                        yield response_data
                        continue

                    # Legacy path: bare token string (vLLM/transformers engines).
                    token = response_data.get('token')
                    if token:
                        yield token
            
            # Idle timeout: no stream event for `timeout` seconds.
            elapsed = time.time() - start_time
            logger.error(
                "Streaming local inference idle timeout",
                request_id=request_id,
                idle_timeout=timeout,
                elapsed_total=elapsed
            )
            
            # Cleanup on timeout
            try:
                self.redis.delete(response_stream)
            except Exception:
                pass  # best-effort cleanup; proceed with timeout error
            
            raise TimeoutError(
                f"Streaming local inference idle timeout: no event for {timeout}s "
                f"(total elapsed {elapsed:.1f}s)"
            )
            
        except GeneratorExit:
            # Client stopped reading - cleanup
            logger.info(
                "Streaming local inference cancelled by client",
                request_id=request_id
            )
            try:
                self.redis.delete(response_stream)
            except Exception:
                pass  # best-effort cleanup on client cancel
            raise
    
    async def infer_async(
        self,
        model_id: str,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 1000,
        timeout: float = 30.0,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Async version of infer() for async contexts.
        
        Note: This requires an async Redis client to be passed during initialization.
        For most Celery workers, use infer() or infer_sync() instead.
        """
        import asyncio
        
        request_id = str(uuid4())
        
        # Prepare request
        request = {
            'request_id': request_id,
            'model_id': model_id,
            'messages': messages,
            'temperature': temperature,
            'max_tokens': max_tokens,
            'worker_id': self.worker_id,
            'timestamp': time.time(),
            **kwargs
        }
        
        logger.info(
            "Sending local inference request (async)",
            request_id=request_id,
            model_id=model_id,
            message_count=len(messages)
        )
        
        try:
            # Publish request to the manager-keyed stream (ADR-0105 §R2)
            request_stream = f'local-inference:{self.manager_id}:requests'
            await self.redis.xadd(
                request_stream,
                {'data': json.dumps(request)}
            )
            
            # Wait for response
            start_time = time.time()
            response_stream = f'local-inference:{self.manager_id}:responses:{request_id}'
            
            while time.time() - start_time < timeout:
                # Non-blocking async read
                result = await self.redis.xread(
                    {response_stream: '0'},
                    count=1,
                    block=100
                )
                
                if result and len(result) > 0:
                    # Extract response
                    stream_data = result[0][1][0]
                    response_bytes = stream_data[1].get(b'data') or stream_data[1].get('data')
                    
                    if isinstance(response_bytes, bytes):
                        response_data = json.loads(response_bytes.decode('utf-8'))
                    else:
                        response_data = json.loads(response_bytes)
                    
                    # Cleanup
                    try:
                        await self.redis.delete(response_stream)
                    except Exception as e:
                        logger.warning("Failed to cleanup response stream", error=str(e))
                    
                    elapsed = time.time() - start_time
                    
                    if not response_data.get('success', False):
                        error_msg = response_data.get('error', 'Unknown error')
                        logger.error(
                            "Local inference failed (async)",
                            request_id=request_id,
                            error=error_msg
                        )
                        raise RuntimeError(f"Local inference failed: {error_msg}")
                    
                    logger.info(
                        "Local inference completed (async)",
                        request_id=request_id,
                        elapsed_seconds=round(elapsed, 3)
                    )
                    
                    return response_data
                
                # Yield to event loop
                await asyncio.sleep(0.01)
            
            # Timeout
            logger.error("Local inference timeout (async)", request_id=request_id)
            try:
                await self.redis.delete(response_stream)
            except Exception:
                pass  # best-effort cleanup on async timeout
            raise TimeoutError(f"Local inference timeout after {timeout}s")
            
        except (TimeoutError, RuntimeError):
            raise
        except Exception as e:
            logger.error(
                "Local inference request failed (async)",
                request_id=request_id,
                error=str(e),
                exc_info=True
            )
            raise RuntimeError(f"Local inference request failed: {e}") from e

