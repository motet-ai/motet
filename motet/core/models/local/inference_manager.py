"""
Motet - Local Inference Manager

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Local inference manager that runs inference operations in dedicated process pool.
    Follows the MCP parent process pattern. Manages multiple
    inference workers, automatic request batching, and model caching. Supports
    CPU, NVIDIA GPU, and Apple Silicon inference.

    Per this manager is hoisted out of the Celery worker lifecycle and runs as an
    independent supervised sibling service (compose service / k8s Deployment). Request and
    response Redis Streams are keyed on a shared ``manager_id`` so one manager serves all
    workers in an orchestrated unit and survives worker restarts while staying warm.

Dependencies:
    - multiprocessing: Process pool for inference workers
    - redis: Redis Streams for request/response communication
    - structlog: Structured logging
    - LocalModelCache: Model loading and caching

Usage:
    # As an independent sibling service (the supported deployment shape, ADR-0105).
    # Workers connect to it via the shared MOTET_LOCAL_INFERENCE_MANAGER_ID.
    python -m motet.core.models.local.inference_manager --manager-id local-inference-default

Notes:
    - Runs independently of Celery workers (like MCPInstanceManager)
    - Manages multiple inference workers across multiple devices (CPU/GPU/Apple Silicon)
    - Automatic request batching for 10-50x throughput improvement
    - Centralized model caching and memory management
    - Canonical-protocol parity: the llama.cpp paths separate
      ``<think>`` reasoning from content, thread sampling overrides + an
      ``enable_thinking`` toggle, return ``usage`` / ``finish_reason``, and recover
      native tool calls (parsing the per-family tool-call text format when the
      embedded-Jinja path leaves them in ``content``). The adapter
      (``adapters/providers/local.py``) maps these onto canonical responses/events.
"""

import os
import json
import time
import asyncio
import multiprocessing as mp
from typing import Any, Dict, List, Optional, cast
from collections import defaultdict
import structlog
import psutil
# HTTP endpoints removed - using Redis-based status publishing instead
# from aiohttp import web

logger = structlog.get_logger(__name__)


# Model name to path registry (must match MODEL_REGISTRY["local"] keys in specs.py)
# Can be overridden by environment variable MOTET_LOCAL_MODEL_PATHS (JSON format)
# Paths are typical GGUF locations; override via MOTET_LOCAL_MODEL_PATHS for your layout.
#
# The base directory defaults to /app/models (the in-container layout) but can be
# overridden with MOTET_LOCAL_MODEL_DIR for local/dev hosts (e.g. an Apple Silicon
# dev box where GGUFs live under <repo>/models). Individual paths can still be
# overridden wholesale via MOTET_LOCAL_MODEL_PATHS.
_DEFAULT_MODEL_DIR = os.getenv("MOTET_LOCAL_MODEL_DIR", "/app/models")

DEFAULT_MODEL_PATHS = {
    # US-origin generative-UI tier (ADR-0114). Small, fast, provenance-clean.
    "phi-4-mini": f"{_DEFAULT_MODEL_DIR}/Phi-4-mini-instruct-Q4_K_M.gguf",
    "gemma-3-4b": f"{_DEFAULT_MODEL_DIR}/gemma-3-4b-it-Q4_K_M.gguf",
    # Refreshed local tier (ADR-0117): current open-weight generations. Small dense
    # models plus one mid-size MoE ("large" option). GGUF filenames follow the
    # common bartowski/unsloth Q4_K_M naming; override via MOTET_LOCAL_MODEL_PATHS.
    "gemma-4-e4b": f"{_DEFAULT_MODEL_DIR}/gemma-4-E4B-it-Q4_K_M.gguf",
    "gemma-4-26b-a4b": f"{_DEFAULT_MODEL_DIR}/gemma-4-26B-A4B-it-Q4_K_M.gguf",
    "hermes-4-14b": f"{_DEFAULT_MODEL_DIR}/Hermes-4-14B-Q4_K_M.gguf",
    "llama-3.1-8b-instruct": f"{_DEFAULT_MODEL_DIR}/Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf",
    "ministral-3-8b-instruct": f"{_DEFAULT_MODEL_DIR}/Ministral-3-8B-Instruct-2512-Q4_K_M.gguf",
    # CN-origin (Alibaba), open-weight. Strong instruction-following + native tool
    # calling, but provenance-gated for restricted deployments (ADR-0115/0116).
    "qwen3-8b-instruct": f"{_DEFAULT_MODEL_DIR}/Qwen3-8B-Q4_K_M.gguf",
}


def get_model_registry() -> Dict[str, str]:
    """
    Get the model name to path registry.
    
    Can be overridden by setting MOTET_LOCAL_MODEL_PATHS environment variable
    to a JSON string mapping model names to paths.
    
    Returns:
        Dict mapping model names to file paths
    """
    custom_paths = os.getenv('MOTET_LOCAL_MODEL_PATHS')
    if custom_paths:
        try:
            return json.loads(custom_paths)
        except json.JSONDecodeError as e:
            logger.warning(
                "Failed to parse MOTET_LOCAL_MODEL_PATHS, using defaults",
                error=str(e)
            )
    return DEFAULT_MODEL_PATHS.copy()


def resolve_model_path(model_name: str) -> Optional[str]:
    """
    Resolve a model name to its file path.
    
    Args:
        model_name: The model name to resolve
        
    Returns:
        The file path to the model, or None if not found
    """
    registry = get_model_registry()
    return registry.get(model_name)


def _tool_names_from_request(request: Dict[str, Any]) -> List[str]:
    """Declared tool names from an OpenAI-style ``tools`` schema list.

    Used to scope text-based tool-call recovery (notably Gemma's ``tool_code``
    Python blocks) to actually-declared tools.
    """
    names: List[str] = []
    for tool in (request.get('tools') or []):
        if not isinstance(tool, dict):
            continue
        fn = tool.get('function')
        if not isinstance(fn, dict):
            continue
        name = fn.get('name')
        if name:
            names.append(name)
    return names


class LocalInferenceManager:
    """
    Manages local inference workers and request routing.
    Supports CPU, NVIDIA GPU, and Apple Silicon inference.
    
    Architecture (like MCPInstanceManager):
    1. Listen on local-inference:{manager_id}:requests Redis Stream
    2. Batch requests with 50ms window (configurable)
    3. Route batches to inference worker processes
    4. Workers process requests and publish to local-inference:{manager_id}:responses:{request_id}
    5. Automatic model loading and caching per device (CPU/GPU/Metal)

    Hoisting (ADR-0105): this manager is a sibling service, decoupled from any single Celery
    worker's lifecycle. Streams are keyed on the shared ``manager_id`` (not per-``worker_id``)
    so one long-lived manager serves all workers in an orchestrated unit and stays warm across
    worker restarts.
    """
    
    def __init__(
        self,
        worker_count: Optional[int] = None,
        device_ids: Optional[List[int]] = None,
        max_memory_gb: float = 24.0,
        cache_size: int = 3,
        batch_wait_ms: int = 50,
        max_batch_size: int = 32,
        worker_id: Optional[str] = None,
        manager_id: Optional[str] = None,
    ):
        """
        Initialize local inference manager.
        
        Args:
            worker_count: Number of inference worker processes (default: auto-detect)
            device_ids: List of device IDs to use (NVIDIA GPU IDs or CPU index, default: from env)
            max_memory_gb: Max memory per worker in GB (GPU VRAM or system RAM)
            cache_size: Max number of models to cache per worker
            batch_wait_ms: Batch window in milliseconds (default: 50ms)
            max_batch_size: Max requests per batch (default: 32)
            worker_id: Bootstrap/observability attribution only (the worker or service that
                started this manager). NOT part of the bus address (ADR-0105 §R2).
            manager_id: Stable Redis Streams routing prefix shared with every
                LocalInferenceClient that this manager serves (ADR-0105 §R2/§R3). One hoisted
                manager owns one manager_id and serves N workers, independent of any single
                worker's lifecycle.
        """
        self.worker_count = worker_count or int(os.getenv('MOTET_GPU_WORKER_COUNT', '1'))
        self.device_ids = device_ids or self._parse_device_ids()
        self.max_memory_gb = max_memory_gb
        self.cache_size = cache_size
        self.batch_wait_ms = batch_wait_ms
        self.max_batch_size = max_batch_size
        
        self.redis: Optional[Any] = None
        self.worker_processes: List[mp.Process] = []
        self.request_queue: List[Dict[str, Any]] = []
        self.last_batch_time = time.time()
        self._running = False
        
        # Health monitoring - using Redis-based status registry
        self.stats = {
            "total_requests": 0,
            "active_batches": 0,
            "errors": 0,
            "start_time": time.time()
        }
        # worker_id: bootstrap/observability attribution (who started this manager).
        self.worker_id = worker_id or os.getenv('CELERY_WORKER_ID', 'default')
        # manager_id: the canonical bus-routing prefix (ADR-0105 §R2/§R3). Every client that
        # this manager serves publishes to ``local-inference:{manager_id}:...`` streams.
        self.manager_id = manager_id or os.getenv('MOTET_LOCAL_INFERENCE_MANAGER_ID', 'local-inference-default')
        
        # Initialize manager status registry
        from motet.core.distributed.manager_status import ManagerStatusRegistry, ManagerType
        self.status_registry = None  # Will be initialized in start() with sync client
        self.manager_type = ManagerType.LOCAL_INFERENCE
        
        logger.info(
            "Initialized local inference manager",
            worker_count=self.worker_count,
            device_ids=self.device_ids,
            batch_wait_ms=self.batch_wait_ms,
            max_batch_size=self.max_batch_size,
            worker_id=self.worker_id,
            manager_id=self.manager_id,
        )
    
    def _parse_device_ids(self) -> List[int]:
        """Parse device IDs from environment (NVIDIA GPU IDs or CPU index)"""
        device_str = os.getenv('MOTET_GPU_DEVICE_IDS', '0')
        return [int(d.strip()) for d in device_str.split(',') if d.strip()]
    
    def start_worker_processes(self):
        """Start inference worker processes (synchronous, called before async loop starts)"""
        logger.info("Starting inference worker processes")
        
        # Start Inference worker processes
        for i in range(self.worker_count):
            device_id = self.device_ids[i % len(self.device_ids)]
            process = mp.Process(
                target=self._run_inference_worker,
                args=(device_id, i),
                daemon=True,
                name=f"local_inference_worker_{i}"
            )
            process.start()
            self.worker_processes.append(process)
            
            logger.info(
                "Started Inference worker process",
                celery_worker_id=self.worker_id,
                subprocess_id=i,
                device_id=device_id,
                pid=process.pid
            )
    
    async def start(self):
        """Start local inference manager (async initialization only)"""
        if self._running:
            logger.warning("Local inference manager already running")
            return
        
        self._running = True
        logger.info("Starting local inference manager async initialization")
        
        # Initialize Redis (use async client like MCPInstanceManager)
        from motet.core.distributed.redis_manager import get_redis_client
        self.redis = get_redis_client("local_inference_manager")
        
        logger.info("Local inference manager initialized, ready to process requests")
    
    async def stop(self):
        """Stop Local inference manager"""
        if not self._running:
            return
        
        logger.info("Stopping Local inference manager")
        self._running = False
        
        # Terminate Inference worker processes
        for process in self.worker_processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
                if process.is_alive():
                    process.kill()
                    process.join()
        
        self.worker_processes.clear()
        
        logger.info("Local inference manager stopped")
    
    async def publish_health_to_redis(self):
        """Publish health metrics to Redis using ManagerStatusRegistry"""
        try:
            # Initialize registry if not done yet (first call from async context)
            if self.status_registry is None:
                from motet.core.distributed.manager_status import ManagerStatusRegistry
                self.status_registry = ManagerStatusRegistry()
            
            # Get manager process metrics
            manager_proc = psutil.Process(os.getpid())
            
            # Count healthy vs unhealthy workers (instances)
            healthy_workers = sum(1 for p in self.worker_processes if p.is_alive())
            unhealthy_workers = len(self.worker_processes) - healthy_workers
            
            # Publish status using the registry.
            # ADR-0105 §R3: key on the shared, canonical ``manager_id`` (the
            # MOTET_LOCAL_INFERENCE_MANAGER_ID routing prefix) — NOT a synthesized
            # ``local_inference-{worker_id}`` id. Post-hoist ``worker_id`` is only a
            # bootstrap-attribution tag, so without this the Instance Managers UI would
            # key this sibling on a meaningless worker id. ``served_workers`` is left to
            # server-side inversion in /managers/status (workers post anonymously to the
            # manager's Redis Streams, so the manager cannot derive its served set).
            self.status_registry.publish_status(
                worker_id=self.worker_id,
                manager_type=self.manager_type,
                status="running" if self._running else "stopped",
                pid=os.getpid(),
                manager_id=self.manager_id,
                instances_total=len(self.worker_processes),
                instances_healthy=healthy_workers,
                instances_unhealthy=unhealthy_workers,
                total_requests=self.stats["total_requests"],
                active_requests=self.stats["active_batches"],
                errors=self.stats["errors"],
                start_time=self.stats["start_time"],
                memory_mb=manager_proc.memory_info().rss / 1024 / 1024,
                cpu_percent=manager_proc.cpu_percent(),
                metadata={
                    "device_ids": self.device_ids,
                    "worker_count": self.worker_count,
                    "request_queue_depth": len(self.request_queue),
                    "max_batch_size": self.max_batch_size,
                    "batch_wait_ms": self.batch_wait_ms
                }
            )
            
        except Exception as e:
            logger.error("Error publishing health to Redis", error=str(e), exc_info=True)
    
    async def _process_requests_loop(self):
        """Main request processing loop"""
        # Define request stream name at start for consistent logging
        request_stream = f'local-inference:{self.manager_id}:requests'
        
        logger.info("Local inference request processing started", 
                   worker_id=self.worker_id,
                   manager_id=self.manager_id,
                   listening_on=request_stream,
                   max_batch_size=self.max_batch_size,
                   batch_wait_ms=self.batch_wait_ms)
        
        last_health_publish = time.time()
        health_publish_interval = 30.0  # Publish health every 30 seconds
        loop_iteration = 0
        last_id = '0'  # Start from beginning to catch any messages already in stream
        
        while self._running:
            try:
                redis = self.redis
                if redis is None:
                    await asyncio.sleep(0.05)
                    continue

                loop_iteration += 1
                
                # Log every 10th iteration to show we're actively listening
                if loop_iteration % 10 == 1:
                    logger.debug("Waiting for requests on stream", 
                               iteration=loop_iteration,
                               stream=request_stream,
                               queue_size=len(self.request_queue),
                               last_id=last_id)
                
                # Read new requests from worker-scoped Redis Stream
                # Use last_id to ensure we don't miss any messages
                result_raw = await redis.xread(
                    {request_stream: last_id},  # Read from last processed message
                    count=self.max_batch_size,
                    block=self.batch_wait_ms
                )
                result = cast(Any, result_raw)

                if result:
                    logger.info("Received messages from stream", 
                               stream=request_stream,
                               num_streams=len(result))
                    
                    # Extract requests
                    for stream_name, messages in result:
                        logger.info("Processing messages from stream", 
                                   stream_name=stream_name,
                                   num_messages=len(messages))
                        
                        for message_id, message_data in messages:
                            request_bytes = message_data.get(b'data') or message_data.get('data')
                            
                            if isinstance(request_bytes, bytes):
                                request = json.loads(request_bytes.decode('utf-8'))
                            else:
                                request = json.loads(request_bytes)
                            
                            logger.info("Received inference request", 
                                       request_id=request.get('request_id'),
                                       model_id=request.get('model_id'),
                                       message_id=message_id)
                            
                            self.request_queue.append(request)
                            self.stats["total_requests"] += 1
                            
                            # Update last_id to continue from this message
                            last_id = message_id
                            
                            # Acknowledge message (remove from stream)
                            await redis.xdel(request_stream, message_id)
                            logger.debug("Acknowledged and removed message from stream", 
                                        message_id=message_id)
                
                # Process batch if ready
                current_time = time.time()
                batch_ready = (
                    len(self.request_queue) >= self.max_batch_size or
                    (len(self.request_queue) > 0 and 
                     (current_time - self.last_batch_time) >= (self.batch_wait_ms / 1000.0))
                )
                
                if batch_ready:
                    await self._process_batch()
                    self.last_batch_time = current_time
                
                # Publish health metrics periodically
                if current_time - last_health_publish >= health_publish_interval:
                    await self.publish_health_to_redis()
                    last_health_publish = current_time
            
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.stats["errors"] += 1
                logger.error("Error in request processing loop", error=str(e), exc_info=True)
                await asyncio.sleep(0.1)  # Backoff on error
    
    async def _process_batch(self):
        """Process current request batch"""
        if not self.request_queue:
            return
        
        batch = self.request_queue[:self.max_batch_size]
        self.request_queue = self.request_queue[self.max_batch_size:]
        
        self.stats["active_batches"] += 1
        logger.info("Processing Local inference batch", batch_size=len(batch))
        
        try:
            # Group requests by model for better efficiency.
            # Buffered requests use 'model_id' (LocalInferenceClient.infer); streaming
            # requests use 'model' (LocalInferenceClient.infer_stream). Normalize both so
            # neither path raises KeyError here.
            requests_by_model = defaultdict(list)
            for request in batch:
                model_id = request.get('model_id') or request.get('model')
                if not model_id:
                    logger.error("local_inference_request_missing_model", request_id=request.get('request_id'))
                    continue
                request['model_id'] = model_id
                requests_by_model[model_id].append(request)
            
            # Process each model group (distribute across GPUs)
            for model_id, requests in requests_by_model.items():
                await self._process_model_batch(model_id, requests)
        finally:
            self.stats["active_batches"] -= 1
    
    async def _process_model_batch(self, model_id: str, requests: List[Dict[str, Any]]):
        """Process batch of requests for a single model"""
        # Simple round-robin distribution across Inference workers
        # TODO: Implement smarter routing based on model location and load
        subprocess_id = hash(model_id) % self.worker_count
        
        batch_data = {
            'model_id': model_id,
            'requests': requests,
            'batch_size': len(requests)
        }
        
        # Publish batch to Inference worker queue
        # Use namespace: local-inference:{manager_id}:subprocess:{subprocess_id}:batches
        stream_key = f'local-inference:{self.manager_id}:subprocess:{subprocess_id}:batches'
        r = self.redis
        if r is None:
            logger.error("local_inference_redis_unavailable_skip_batch", model_id=model_id)
            return
        await r.xadd(
            stream_key,
            {'data': json.dumps(batch_data)}
        )
    
    def _run_inference_worker(self, device_id: int, subprocess_id: int):
        """
        Inference worker process (runs in separate process).
        This is where actual inference operations (CPU/GPU/Metal) happen!
        
        Args:
            device_id: Physical device ID (GPU index or CPU index)
            subprocess_id: Local subprocess index (0, 1, 2... within this manager)
        """
        # Set CUDA device for this process (NVIDIA GPUs only)
        os.environ['CUDA_VISIBLE_DEVICES'] = str(device_id)
        
        logger.info(
            "Inference worker process started",
            celery_worker_id=self.worker_id,
            subprocess_id=subprocess_id,
            device_id=device_id,
            pid=os.getpid()
        )
        
        try:
            # Initialize Redis (sync client in worker process)
            from motet.core.distributed.redis_manager import UnifiedRedisManager
            redis_manager = UnifiedRedisManager()
            redis_client = redis_manager.get_sync_client()
            
            # Initialize local model cache
            from .model_cache import LocalModelCache
            model_cache = LocalModelCache(
                device_id=device_id,
                max_memory_gb=self.max_memory_gb,
                cache_size=self.cache_size,
                engine="auto"  # Auto-detect best engine
            )
            
            logger.info(
                "Inference worker initialized",
                celery_worker_id=self.worker_id,
                subprocess_id=subprocess_id,
                device_id=device_id,
                engine=model_cache.engine
            )
            
            # Process batches
            # Use namespace: local-inference:{manager_id}:subprocess:{subprocess_id}:batches
            stream_key = f'local-inference:{self.manager_id}:subprocess:{subprocess_id}:batches'
            last_id = '0'  # Start from beginning to process any backlog
            logger.info(f"🔄 Inference subprocess {subprocess_id} (worker {self.worker_id}) starting batch processing loop on stream {stream_key}...")
            while True:
                try:
                    # Read batch requests
                    result_raw = redis_client.xread(
                        {stream_key: last_id},
                        count=1,
                        block=100  # 100ms
                    )
                    result = cast(Any, result_raw)

                    if result:
                        logger.debug(f"📥 Subprocess {subprocess_id} received batch, processing...")
                        for stream_name, messages in result:
                            for message_id, message_data in messages:
                                logger.info(f"📦 Processing batch message_id={message_id}, last_id was {last_id}")
                                batch_bytes = message_data.get(b'data') or message_data.get('data')
                                
                                if isinstance(batch_bytes, bytes):
                                    batch_data = json.loads(batch_bytes.decode('utf-8'))
                                else:
                                    batch_data = json.loads(batch_bytes)
                                
                                logger.info(f"🔧 About to process batch for model: {batch_data.get('model_id')}")
                                self._process_batch_sync(
                                    model_cache,
                                    batch_data,
                                    redis_client
                                )
                                logger.info(f"✅ Batch processing completed for message_id={message_id}")
                                
                                # Update cursor to move past this message
                                last_id = message_id
                                logger.debug(f"📍 Updated last_id to {last_id}")
                                
                                # Acknowledge batch
                                redis_client.xdel(
                                    stream_key,
                                    message_id
                                )
                                logger.debug(f"🗑️ Deleted message {message_id} from stream")
                    else:
                        # No messages available (timeout)
                        pass
                
                except KeyboardInterrupt:
                    logger.info("Inference worker interrupted", subprocess_id=subprocess_id, celery_worker_id=self.worker_id)
                    break
                except Exception as e:
                    logger.error(
                        "Error in Inference worker",
                        subprocess_id=subprocess_id,
                        celery_worker_id=self.worker_id,
                        error=str(e),
                        exc_info=True
                    )
                    time.sleep(0.1)  # Backoff on error
        
        except Exception as e:
            logger.error(
                "Fatal error in Inference worker",
                subprocess_id=subprocess_id,
                celery_worker_id=self.worker_id,
                error=str(e),
                exc_info=True
            )
        finally:
            logger.info("Inference worker process exiting", subprocess_id=subprocess_id, celery_worker_id=self.worker_id)
    
    def _process_batch_sync(
        self,
        model_cache,
        batch_data: Dict[str, Any],
        redis_client
    ):
        """Process batch of inference requests (synchronous)"""
        model_id = batch_data['model_id']
        requests = batch_data['requests']
        
        try:
            # Resolve model name to path using registry
            model_path = resolve_model_path(model_id)
            
            if not model_path:
                # No model path found in registry
                error_msg = f"Model '{model_id}' not found in model registry. Available models: {list(get_model_registry().keys())}"
                logger.error(error_msg, model_id=model_id)
                
                # Publish error for all requests in batch
                for request in requests:
                    response = {
                        'request_id': request['request_id'],
                        'result': {'error': error_msg},
                        'model_id': model_id,
                        'success': False
                    }
                    response_stream = f'local-inference:{self.manager_id}:responses:{request["request_id"]}'
                    redis_client.xadd(
                        response_stream,
                        {'data': json.dumps(response)}
                    )
                return
            
            logger.info("Resolved model path", model_id=model_id, model_path=model_path)
            
            # Load model (cached) with resolved path
            logger.info("🔄 Starting model load from cache", model_id=model_id, model_path=model_path)
            model, metadata = model_cache.get_or_load_model_sync(model_id, model_path=model_path)
            logger.info("✅ Model loaded successfully", model_id=model_id, metadata=metadata)
            
            # Process each request
            for request in requests:
                try:
                    # Check if streaming is requested
                    if request.get('stream', False):
                        # STREAMING: Publish tokens incrementally
                        self._run_inference_stream(
                            model,
                            request,
                            model_cache.engine,
                            redis_client
                        )
                    else:
                        # BUFFERED: Run inference and publish complete response
                        start_time = time.time()
                        result = self._run_inference_sync(
                            model,
                            request,
                            model_cache.engine
                        )
                        elapsed = time.time() - start_time
                        
                        # Publish response. Expose text/finish_reason at the top
                        # level (in addition to the nested 'result') so consumers
                        # like LocalAdapter can read result['text'] directly.
                        rdict = result if isinstance(result, dict) else {}
                        response = {
                            'request_id': request['request_id'],
                            'result': result,
                            'text': rdict.get('text', ''),
                            'finish_reason': rdict.get('finish_reason'),
                            'reasoning': rdict.get('reasoning'),
                            'usage': rdict.get('usage'),
                            'tool_calls': rdict.get('tool_calls'),
                            'model_id': model_id,
                            'elapsed_seconds': elapsed,
                            'success': True
                        }
                        
                        response_stream = f'local-inference:{self.manager_id}:responses:{request["request_id"]}'
                        redis_client.xadd(
                            response_stream,
                            {'data': json.dumps(response)}
                        )
                        
                        logger.debug(
                            "Inference completed",
                            request_id=request['request_id'],
                            elapsed=round(elapsed, 3)
                        )
                
                except Exception as e:
                    # Publish error response
                    error_response = {
                        'request_id': request['request_id'],
                        'error': str(e),
                        'success': False
                    }
                    
                    response_stream = f'local-inference:{self.manager_id}:responses:{request["request_id"]}'
                    redis_client.xadd(
                        response_stream,
                        {'data': json.dumps(error_response)}
                    )
                    
                    logger.error(
                        "Inference failed",
                        request_id=request['request_id'],
                        error=str(e),
                        exc_info=True
                    )
        
        except Exception as e:
            logger.error(
                "Failed to process batch",
                model_id=model_id,
                error=str(e),
                exc_info=True
            )
            
            # Send error responses for all requests in batch
            for request in requests:
                error_response = {
                    'request_id': request['request_id'],
                    'error': f"Batch processing failed: {str(e)}",
                    'success': False
                }
                
                try:
                    response_stream = f'local-inference:{self.manager_id}:responses:{request["request_id"]}'
                    redis_client.xadd(
                        response_stream,
                        {'data': json.dumps(error_response)}
                    )
                except Exception as publish_error:
                    logger.error(
                        "Failed to publish error response",
                        request_id=request['request_id'],
                        error=str(publish_error)
                    )
    
    def _run_inference_sync(
        self,
        model: Any,
        request: Dict[str, Any],
        engine: str
    ) -> Dict[str, Any]:
        """Run model inference (engine-specific)"""
        if engine == "vllm":
            return self._run_vllm_inference(model, request)
        elif engine == "llama_cpp":
            return self._run_llama_cpp_inference(model, request)
        elif engine == "transformers":
            return self._run_transformers_inference(model, request)
        else:
            raise ValueError(f"Unsupported engine: {engine}")
    
    def _run_inference_stream(
        self,
        model: Any,
        request: Dict[str, Any],
        engine: str,
        redis_client
    ):
        """
        Run streaming inference and publish tokens incrementally to Redis Streams.
        
        Publishes each token as it's generated, then sends a completion marker.
        """
        request_id = request['request_id']
        start_time = time.time()
        token_count = 0
        
        try:
            logger.info(
                "Starting streaming inference",
                request_id=request_id,
                engine=engine
            )
            
            # Choose streaming method based on engine
            if engine == "vllm":
                token_generator = self._run_vllm_inference_stream(model, request)
            elif engine == "llama_cpp":
                token_generator = self._run_llama_cpp_inference_stream(model, request)
            elif engine == "transformers":
                token_generator = self._run_transformers_inference_stream(model, request)
            else:
                raise ValueError(f"Unsupported engine: {engine}")
            
            # Publish tokens/events as they arrive.
            #
            # llama.cpp now yields typed event dicts ({'type': 'text'|'tool_call_delta'|
            # 'tool_call_complete'|'final', ...}); vLLM/transformers still yield bare
            # token strings. Handle both: pass typed events through verbatim, wrap raw
            # strings as legacy {'token': ...}, and capture the terminal 'final' event
            # to enrich the completion marker with finish_reason/usage.
            response_stream = f'local-inference:{self.manager_id}:responses:{request_id}'
            final_meta: Dict[str, Any] = {}
            for item in token_generator:
                if isinstance(item, dict):
                    if item.get('type') == 'final':
                        final_meta = item
                        continue
                    if item.get('type') in (None, 'text') and item.get('text'):
                        token_count += 1
                    event_response = {'request_id': request_id, 'success': True, **item}
                    redis_client.xadd(
                        response_stream,
                        {'data': json.dumps(event_response)}
                    )
                else:
                    token_count += 1
                    redis_client.xadd(
                        response_stream,
                        {'data': json.dumps({'request_id': request_id, 'token': item, 'success': True})}
                    )

            # Send completion marker (carry finish_reason/usage from the final event)
            elapsed = time.time() - start_time
            completion_response = {
                'request_id': request_id,
                'done': True,
                'success': True,
                'token_count': token_count,
                'elapsed_seconds': elapsed,
                'finish_reason': final_meta.get('finish_reason'),
                'usage': final_meta.get('usage'),
            }
            
            response_stream = f'local-inference:{self.manager_id}:responses:{request_id}'
            redis_client.xadd(
                response_stream,
                {'data': json.dumps(completion_response)}
            )
            
            logger.info(
                "Streaming inference completed",
                request_id=request_id,
                token_count=token_count,
                elapsed_seconds=round(elapsed, 3)
            )
        
        except Exception as e:
            # Publish error
            error_response = {
                'request_id': request_id,
                'error': str(e),
                'success': False
            }
            
            response_stream = f'local-inference:{self.manager_id}:responses:{request_id}'
            redis_client.xadd(
                response_stream,
                {'data': json.dumps(error_response)}
            )
            
            logger.error(
                "Streaming inference failed",
                request_id=request_id,
                error=str(e),
                exc_info=True
            )
            raise
    
    def _run_vllm_inference(self, model: Any, request: Dict[str, Any]) -> Dict[str, Any]:
        """Run inference with vLLM"""
        from vllm import SamplingParams  # type: ignore[reportMissingImports]
        
        # Extract prompt from messages
        prompt = self._messages_to_prompt(request['messages'])
        
        sampling_params = SamplingParams(
            temperature=request.get('temperature', 0.7),
            max_tokens=request.get('max_tokens', 1000),
            top_p=0.95
        )
        
        outputs = model.generate([prompt], sampling_params)
        text = outputs[0].outputs[0].text
        
        return {
            'text': text,
            'finish_reason': 'stop'
        }
    
    def _run_llama_cpp_inference(self, model: Any, request: Dict[str, Any]) -> Dict[str, Any]:
        """
        Run inference with llama.cpp.

        ADR-0114 Phase 1: use ``create_chat_completion`` so the per-model chat
        template embedded in the GGUF is applied automatically (Gemma 3 uses
        ``<start_of_turn>`` turns; Phi-4 uses ``<|im_start|>`` tags). This replaces
        the previous naive ``_messages_to_prompt()`` concatenation, which fed a
        format no instruct model was trained on and degraded output quality.

        An optional grammar (GBNF, compiled from JSON Schema when provided)
        constrains decoding for guaranteed-parseable structured output. If the
        GGUF lacks chat-template metadata, fall back to raw completion.

        ADR-0064 parity: separates ``<think>`` reasoning from user-facing content,
        forwards native tool schemas, and returns raw ``usage`` / ``finish_reason``
        / ``tool_calls`` for the adapter to map onto the canonical protocol.
        """
        from .reasoning import split_reasoning
        from .profiles import profile_for_model

        model_id = request.get('model_id') or request.get('model')
        profile = profile_for_model(model_id)
        tools_requested = bool(request.get('tools'))
        messages = profile.normalize_messages(request.get('messages') or [])
        messages = profile.apply_thinking_control(messages, request.get('enable_thinking', True))
        messages = profile.apply_tool_schemas(messages, request)
        grammar = self._build_llama_grammar(request)
        kwargs = self._llama_sampling_kwargs(request, model_id)
        if grammar is not None:
            kwargs['grammar'] = grammar
        profile.apply_tool_kwargs(request, kwargs)

        try:
            output = model.create_chat_completion(messages=messages, **kwargs)
            choice = output['choices'][0]
            message = choice.get('message') or {}
            raw_text = message.get('content') or ''
            # Prefer a native reasoning_content field if the runtime exposes it;
            # otherwise parse the inline <think> block ourselves.
            reasoning = message.get('reasoning_content')
            if reasoning:
                text = raw_text
            else:
                text, reasoning = split_reasoning(raw_text)
            finish_reason = choice.get('finish_reason') or 'stop'

            raw_tool_calls = message.get('tool_calls')
            # llama.cpp's embedded-Jinja path renders tool schemas but does not
            # always parse the model's tool-call output; recover them from the
            # per-family text format when tools were requested (ADR-0115).
            if not raw_tool_calls and tools_requested:
                text, extracted = profile.extract_tool_calls(
                    text, tool_names=_tool_names_from_request(request)
                )
                if extracted:
                    raw_tool_calls = extracted
                    finish_reason = 'tool_calls'

            result: Dict[str, Any] = {
                'text': text,
                'finish_reason': finish_reason,
                'usage': output.get('usage'),
            }
            if reasoning:
                result['reasoning'] = reasoning
            if raw_tool_calls:
                result['tool_calls'] = raw_tool_calls
            return result
        except Exception as exc:
            logger.warning(
                "create_chat_completion unavailable; falling back to raw completion",
                error=str(exc),
            )
            prompt = self._messages_to_prompt(messages, model_id=model_id)
            kwargs.pop('grammar', None)
            kwargs.pop('tools', None)
            kwargs.pop('tool_choice', None)
            output = model(prompt, echo=False, **kwargs)
            raw_text = output['choices'][0]['text']
            text, reasoning = split_reasoning(raw_text)
            result = {
                'text': text,
                'finish_reason': 'stop',
                'usage': output.get('usage'),
            }
            if reasoning:
                result['reasoning'] = reasoning
            return result
    
    def _run_transformers_inference(self, model: Any, request: Dict[str, Any]) -> Dict[str, Any]:
        """Run inference with transformers"""
        import torch
        
        model_obj, tokenizer = model
        
        # Extract prompt from messages
        prompt = self._messages_to_prompt(request['messages'])
        
        inputs = tokenizer(prompt, return_tensors="pt").to(model_obj.device)
        
        with torch.no_grad():
            outputs = model_obj.generate(
                **inputs,
                max_new_tokens=request.get('max_tokens', 1000),
                temperature=request.get('temperature', 0.7),
                do_sample=True,
                top_p=0.95
            )
        
        response = tokenizer.decode(outputs[0], skip_special_tokens=True)
        # Remove prompt from response
        response = response[len(prompt):].strip()
        
        return {
            'text': response,
            'finish_reason': 'stop'
        }
    
    def _run_vllm_inference_stream(self, model: Any, request: Dict[str, Any]):
        """Run streaming inference with vLLM"""
        from vllm import SamplingParams  # type: ignore[reportMissingImports]
        
        # Extract prompt from messages
        prompt = self._messages_to_prompt(request['messages'])
        
        sampling_params = SamplingParams(
            temperature=request.get('temperature', 0.7),
            max_tokens=request.get('max_tokens', 1000),
            top_p=0.95
        )
        
        # vLLM generates outputs - we need to manually yield text chunks
        # Since vLLM doesn't have true token-by-token streaming in the same way,
        # we'll generate and split the output into smaller chunks
        outputs = model.generate([prompt], sampling_params)
        text = outputs[0].outputs[0].text
        
        # Yield text in word-sized chunks (simulated streaming for vLLM)
        # Note: vLLM Enterprise has true streaming, but open-source doesn't expose it easily
        words = text.split()
        for i, word in enumerate(words):
            # Add space after each word except the last
            yield word + (' ' if i < len(words) - 1 else '')
    
    def _run_llama_cpp_inference_stream(self, model: Any, request: Dict[str, Any]):
        """
        Run streaming inference with llama.cpp (TRUE STREAMING).

        ADR-0114 Phase 1: stream via ``create_chat_completion`` so the per-model
        chat template is applied (see ``_run_llama_cpp_inference``). Falls back to
        raw completion streaming only if no token was produced and the chat path
        failed (e.g. a GGUF without chat-template metadata).

        ADR-0064 parity: yields typed event dicts rather than bare token strings:
        ``{'type': 'text', 'text': ...}`` for content (still containing any
        ``<think>`` tags so the adapter's stream router can classify them),
        ``{'type': 'tool_call_delta'|'tool_call_complete', ...}`` for native tool
        calls, and a terminal ``{'type': 'final', 'finish_reason', 'usage'}``.

        ADR-0115: when tools are requested, content streams incrementally through
        a ``ThinkStreamRouter`` (thinking emitted live as ``{'type': 'thinking'}``)
        and a ``ToolCallStreamGate`` that withholds only tool-call markup (e.g.
        Qwen3 ``<tool_call>{...}</tool_call>``, which llama.cpp's Jinja path
        leaves in ``content``). The withheld text is parsed at end of stream into
        canonical tool-call events. This replaces whole-turn buffering, which
        suppressed all streaming and made slow local turns race the client
        timeout with zero output.
        """
        from .reasoning import (
            ThinkStreamRouter,
            ToolCallStreamGate,
            looks_like_unmatched_tool_call_markup,
        )
        from .profiles import profile_for_model

        model_id = request.get('model_id') or request.get('model')
        profile = profile_for_model(model_id)
        tools_requested = bool(request.get('tools'))
        messages = profile.normalize_messages(request.get('messages') or [])
        messages = profile.apply_thinking_control(messages, request.get('enable_thinking', True))
        messages = profile.apply_tool_schemas(messages, request)
        grammar = self._build_llama_grammar(request)
        kwargs = self._llama_sampling_kwargs(request, model_id)
        if grammar is not None:
            kwargs['grammar'] = grammar
        profile.apply_tool_kwargs(request, kwargs)

        yielded = False
        finish_reason: Optional[str] = None
        usage: Optional[Dict[str, Any]] = None
        # Accumulate streamed tool-call fragments by index so we can emit a
        # consolidated tool_call_complete at the end of the stream.
        tool_acc: Dict[int, Dict[str, Any]] = {}
        # Full non-thinking model text, accumulated pre-gate. The stream gate
        # withholds known sentinels (including fenced JSON/Python), and this
        # buffer remains as a last-chance recovery path for other embedded
        # tool-call shapes that do not have a reliable leading marker.
        text_acc: List[str] = []
        # Tool turns stream live: thinking is classified incrementally and only
        # tool-call markup is withheld for end-of-stream parsing (see docstring).
        think_router = ThinkStreamRouter()
        tool_gate = ToolCallStreamGate()

        def _route_tool_turn_token(token: str):
            """Split a raw token into live thinking/text events, gating tool markup."""
            for channel, piece in think_router.feed(token):
                if channel == 'thinking':
                    yield {'type': 'thinking', 'text': piece}
                else:
                    text_acc.append(piece)
                    emit = tool_gate.feed(piece)
                    if emit:
                        yield {'type': 'text', 'text': emit}

        try:
            stream = model.create_chat_completion(messages=messages, stream=True, **kwargs)
            for chunk in stream:
                choice = (chunk.get('choices') or [{}])[0]
                delta = choice.get('delta') or {}

                token = delta.get('content')
                if token:
                    yielded = True
                    if tools_requested:
                        yield from _route_tool_turn_token(token)
                    else:
                        yield {'type': 'text', 'text': token}

                for tc in (delta.get('tool_calls') or []):
                    yielded = True
                    idx = tc.get('index', 0)
                    acc = tool_acc.setdefault(idx, {'id': None, 'name': None, 'arguments': ''})
                    if tc.get('id'):
                        acc['id'] = tc['id']
                    fn = tc.get('function') or {}
                    if fn.get('name'):
                        acc['name'] = fn['name']
                    arg_delta = fn.get('arguments') or ''
                    if arg_delta:
                        acc['arguments'] += arg_delta
                    yield {
                        'type': 'tool_call_delta',
                        'index': idx,
                        'call_id': acc['id'] or f'call_{idx}',
                        'tool_name': acc['name'],
                        'arguments_delta': arg_delta,
                    }

                if choice.get('finish_reason'):
                    finish_reason = choice['finish_reason']
                if chunk.get('usage'):
                    usage = chunk['usage']

            # End of stream: drain the router, then resolve gated tool markup.
            extracted: List[Dict[str, Any]] = []
            if tools_requested:
                for channel, piece in think_router.flush():
                    if channel == 'thinking':
                        yield {'type': 'thinking', 'text': piece}
                    else:
                        text_acc.append(piece)
                        emit = tool_gate.feed(piece)
                        if emit:
                            yield {'type': 'text', 'text': emit}
                tail, held = tool_gate.flush()
                if tail:
                    yield {'type': 'text', 'text': tail}
                if held:
                    leftover = held
                    if not tool_acc:
                        leftover, extracted = profile.extract_tool_calls(
                            held, tool_names=_tool_names_from_request(request)
                        )
                        if (
                            leftover
                            and not extracted
                            and looks_like_unmatched_tool_call_markup(leftover)
                        ):
                            # The model emitted tool-call-shaped markup whose names
                            # match no declared tool (small local models imitate the
                            # example invocations in tool descriptions). Suppress it
                            # rather than leaking internal-looking JSON to the user
                            # as text (ADR-0115 recovery hardening).
                            logger.warning(
                                "local_tool_call_markup_unmatched_suppressed",
                                model_id=model_id,
                                preview=leftover[:200],
                            )
                            leftover = ""
                    if leftover and not extracted:
                        # Gated text that wasn't a parseable tool call (e.g. a
                        # bare-JSON answer); emit it rather than dropping it.
                        yield {'type': 'text', 'text': leftover}
                # Recovery fallback: if a model narrates first and emits a call
                # in an unsupported embedded shape, it may not have been held by
                # the sentinel gate. Re-parse the full streamed text only when no
                # other tool-call path matched; declared-name scoping keeps normal
                # prose answers unaffected.
                if not tool_acc and not extracted:
                    full_text = "".join(text_acc)
                    if full_text.strip():
                        _, recovered = profile.extract_tool_calls(
                            full_text, tool_names=_tool_names_from_request(request)
                        )
                        if recovered:
                            extracted = recovered
                for i, tc in enumerate(extracted):
                    fn = tc.get('function') or {}
                    yield {
                        'type': 'tool_call_complete',
                        'index': i,
                        'call_id': tc.get('id') or f'call_{i}',
                        'tool_name': fn.get('name'),
                        'arguments_json': fn.get('arguments') or '{}',
                    }

            for idx in sorted(tool_acc.keys()):
                acc = tool_acc[idx]
                if not acc.get('name'):
                    continue
                yield {
                    'type': 'tool_call_complete',
                    'index': idx,
                    'call_id': acc['id'] or f'call_{idx}',
                    'tool_name': acc['name'],
                    'arguments_json': acc['arguments'] or '{}',
                }
            # Tool calls win over a model-reported 'stop' (text-recovered calls
            # arrive after llama.cpp already reported the natural stop).
            if tool_acc or extracted:
                finish_reason = 'tool_calls'
            yield {'type': 'final', 'finish_reason': finish_reason or 'stop', 'usage': usage}
            return
        except Exception as exc:
            if yielded:
                # Already emitted partial output; surfacing a retry would duplicate text.
                raise
            logger.warning(
                "create_chat_completion stream unavailable; falling back to raw completion",
                error=str(exc),
            )

        prompt = self._messages_to_prompt(messages, model_id=model_id)
        kwargs.pop('grammar', None)
        kwargs.pop('tools', None)
        kwargs.pop('tool_choice', None)
        for output in model(prompt, echo=False, stream=True, **kwargs):
            token = output['choices'][0]['text']
            if token:
                yield {'type': 'text', 'text': token}
        yield {'type': 'final', 'finish_reason': 'stop', 'usage': None}
    
    def _run_transformers_inference_stream(self, model: Any, request: Dict[str, Any]):
        """Run streaming inference with transformers (TRUE STREAMING)"""
        import torch
        from transformers import TextIteratorStreamer
        from threading import Thread
        
        model_obj, tokenizer = model
        
        # Extract prompt from messages
        prompt = self._messages_to_prompt(request['messages'])
        
        inputs = tokenizer(prompt, return_tensors="pt").to(model_obj.device)
        
        # Create streamer
        streamer = TextIteratorStreamer(
            tokenizer,
            skip_prompt=True,
            skip_special_tokens=True
        )
        
        # Run generation in separate thread so we can stream tokens
        generation_kwargs = dict(
            inputs,
            max_new_tokens=request.get('max_tokens', 1000),
            temperature=request.get('temperature', 0.7),
            do_sample=True,
            top_p=0.95,
            streamer=streamer
        )
        
        thread = Thread(target=model_obj.generate, kwargs=generation_kwargs)
        thread.start()
        
        # Yield tokens as they arrive
        for text in streamer:
            if text:
                yield text
        
        # Wait for generation to complete
        thread.join()
    
    def _llama_sampling_kwargs(
        self, request: Dict[str, Any], model_id: Optional[str]
    ) -> Dict[str, Any]:
        """Build llama.cpp sampling kwargs from the request (ADR-0064 passthrough).

        Threads caller-supplied sampling controls (``top_p``, ``top_k``,
        ``repeat_penalty``, ``seed``) instead of hardcoding them, and merges any
        caller ``stop`` overrides with the family end-of-turn stop sequences so the
        runaway-generation safety net is preserved.
        """
        from .profiles import profile_for_model

        kwargs: Dict[str, Any] = dict(
            temperature=request.get('temperature', 0.7),
            max_tokens=request.get('max_tokens', 1000),
            top_p=request.get('top_p', 0.95),
        )
        for name in ('top_k', 'repeat_penalty', 'seed'):
            value = request.get(name)
            if value is not None:
                kwargs[name] = value

        stop = profile_for_model(model_id).stop_sequences()
        extra_stop = request.get('stop')
        if isinstance(extra_stop, str):
            extra_stop = [extra_stop]
        if extra_stop:
            stop = list(dict.fromkeys([*stop, *extra_stop]))
        if stop:
            kwargs['stop'] = stop
        return kwargs

    def _build_llama_grammar(self, request: Dict[str, Any]):
        """
        Build an optional llama.cpp grammar for constrained decoding (ADR-0114).

        The canonical/portable form is JSON Schema (compiled to GBNF via
        llama.cpp's ``json-schema-to-grammar``); a raw GBNF string is also
        accepted as an escape hatch. Returns ``None`` when no constraint is
        requested or if compilation fails (decoding then proceeds unconstrained).
        """
        gbnf = request.get('gbnf_grammar')
        json_schema = request.get('json_schema')
        if not gbnf and not json_schema:
            return None
        try:
            from llama_cpp import LlamaGrammar  # type: ignore[reportMissingImports]
            if gbnf:
                return LlamaGrammar.from_string(gbnf)
            return LlamaGrammar.from_json_schema(json.dumps(json_schema))
        except Exception as exc:
            logger.warning(
                "Failed to build llama grammar; proceeding unconstrained",
                error=str(exc),
            )
            return None

    def _messages_to_prompt(
        self, messages: List[Dict[str, Any]], model_id: Optional[str] = None
    ) -> str:
        """
        Fallback prompt formatter for non-chat completion paths.

        NOTE: The llama.cpp path now uses ``create_chat_completion`` so the
        model's own chat template is applied (ADR-0114). This helper remains only
        as a fallback for engines/GGUFs without chat-template metadata and for
        the vLLM/transformers paths until they adopt their native templating.
        For ChatML-family models (Qwen/Phi), keep the raw fallback aligned with
        their turn delimiters so the family stop sequence (<|im_end|>) still
        terminates generation instead of encouraging synthetic ``User:`` /
        ``Assistant:`` transcript continuation.
        """
        from .profiles import profile_for_model

        return profile_for_model(model_id).fallback_prompt(messages)


# Entry point for standalone / sibling-service process (ADR-0042 Option 2; hoisted per ADR-0105).
#
# This module is launched as an independent supervised service (a sibling docker-compose
# service or k8s Deployment), NOT spawned by a Celery worker. Its lifecycle is owned by the
# orchestrator, so it survives worker restarts while keeping models warm. Workers reach it via
# the shared --manager-id (== MOTET_LOCAL_INFERENCE_MANAGER_ID) Redis Streams routing prefix.
if __name__ == '__main__':
    import asyncio
    import argparse
    
    # Parse command line arguments
    parser = argparse.ArgumentParser(description='Local Inference Manager - Standalone Sibling Service')
    parser.add_argument(
        '--manager-id',
        type=str,
        default=os.getenv('MOTET_LOCAL_INFERENCE_MANAGER_ID', 'local-inference-default'),
        help='Stable Redis Streams routing prefix shared with the clients this manager serves '
             '(ADR-0105 §R2/§R3). Must match MOTET_LOCAL_INFERENCE_MANAGER_ID on the workers.',
    )
    parser.add_argument(
        '--worker-id',
        type=str,
        default=None,
        help='Observability/bootstrap attribution only. Defaults to the manager-id.',
    )
    args = parser.parse_args()
    
    # Default the status-attribution worker_id to the manager_id when not explicitly set, so the
    # ops status surface shows the manager's stable identity rather than a per-worker id.
    bootstrap_worker_id = args.worker_id or args.manager_id
    
    logger.info(
        "Starting LocalInferenceManager as standalone sibling service",
        manager_id=args.manager_id,
        worker_id=bootstrap_worker_id,
    )
    
    # Create manager instance with the shared manager_id from command line / env
    manager = LocalInferenceManager(worker_id=bootstrap_worker_id, manager_id=args.manager_id)
    
    # Start inference worker subprocesses first (synchronous to avoid multiprocessing issues)
    manager.start_worker_processes()
    
    # Run async event loop for request coordination
    async def run_manager():
        """Run the manager with async coordination and Redis-based status publishing"""
        try:
            # Initialize async components (Redis, etc.)
            await manager.start()
            
            # NOTE: HTTP metrics server removed - using Redis-based ManagerStatusRegistry instead
            # Manager will publish status via publish_health_to_redis() in the processing loop
            logger.info("✅ LocalInferenceManager initialized with Redis-based status publishing")
            
            # Start request processing loop as background task
            processing_task = asyncio.create_task(manager._process_requests_loop())
            
            # Wait for task to complete (runs until interrupted)
            await processing_task
            
        except asyncio.CancelledError:
            logger.info("LocalInferenceManager cancelled")
            await manager.stop()
        except Exception as e:
            logger.error("LocalInferenceManager error", error=str(e), exc_info=True)
            await manager.stop()
            raise
    
    # Run the async manager
    try:
        asyncio.run(run_manager())
    except KeyboardInterrupt:
        logger.info("LocalInferenceManager interrupted by user")
    except Exception as e:
        logger.error("LocalInferenceManager failed", error=str(e), exc_info=True)
        raise

