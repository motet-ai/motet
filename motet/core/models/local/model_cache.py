"""
Motet - Local Model Cache

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Local model cache with LRU eviction for efficient memory management.
    Supports local inference on CPU, NVIDIA GPUs, and Apple Silicon.
    Supports vLLM, llama.cpp, and transformers inference engines with automatic
    engine selection based on platform.

Dependencies:
    - vllm: High-throughput LLM inference engine (optional, NVIDIA)
    - llama-cpp-python: llama.cpp Python bindings (optional, Apple Silicon/NVIDIA/CPU)
    - transformers: Hugging Face transformers library (optional, fallback)
    - torch: PyTorch for model loading and device operations

Usage:
    from motet.core.models.local.model_cache import LocalModelCache
    
    cache = LocalModelCache(device_id=0, max_memory_gb=24, engine="auto")
    model, metadata = cache.get_or_load_model_sync("meta-llama/Llama-2-7b-chat-hf")
    
    # Use model for inference...

Notes:
    - Implements LRU eviction when memory is full (GPU VRAM or system RAM)
    - Supports model quantization (int8, int4) for memory efficiency
    - Thread-safe model access using WorkerLock
    - Auto-engine selection: vLLM for NVIDIA, llama.cpp for Apple Silicon/CPU
"""

import os
import gc
import time
import platform
from typing import Dict, Optional, Any, Tuple
from collections import OrderedDict
import structlog
from pydantic import BaseModel, Field

from motet.core.workers.concurrency_primitives import WorkerLock

logger = structlog.get_logger(__name__)


# --- Context window sizing for llama.cpp (ADR-0114/0115) ---
#
# llama.cpp must allocate a fixed context window (``n_ctx``) at load time. The old
# 4096 default was far too small for tool-using turns: a single agentic step feeds
# the system prompt + every tool's JSON schema (10+ tools ~= 3k+ tokens) + the
# conversation history + a tool *result* (a fetched web page is commonly 2k+
# tokens). The combined prompt overruns 4096 and llama.cpp raises
# ``ValueError: Requested tokens (N) exceed context window of 4096``; the streaming
# path then falls back to a raw-completion prompt of the *same* oversized length,
# or (when it just barely fits) the model is crammed to the ceiling and small
# models degrade to a canned refusal ("I can't browse websites") instead of using
# the tool result. The ADR-0117 tier models all train on far larger contexts
# (phi-4-mini: 128k), so a generous bounded default fits real tool-result turns
# while keeping KV-cache memory predictable. Override with ``MOTET_LOCAL_N_CTX``.
DEFAULT_LLAMA_N_CTX = 16384


def _resolve_default_n_ctx() -> int:
    """Resolve the llama.cpp context window, honoring ``MOTET_LOCAL_N_CTX``."""
    raw = os.environ.get("MOTET_LOCAL_N_CTX")
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
            logger.warning("local.n_ctx.invalid_env", value=raw, fallback=DEFAULT_LLAMA_N_CTX)
        except ValueError:
            logger.warning("local.n_ctx.invalid_env", value=raw, fallback=DEFAULT_LLAMA_N_CTX)
    return DEFAULT_LLAMA_N_CTX


# --- Per-family chat formatting + stop-token resolution for llama.cpp (ADR-0114) ---
#
# llama-cpp-python's create_chat_completion needs the *correct* chat handler to
# emit each model family's turn delimiters and, critically, to treat that family's
# end-of-turn token as a stop. When a GGUF lacks usable chat-template metadata,
# llama.cpp silently falls back to a Llama-2 ("[INST]") format; an instruct model
# trained on different delimiters then never emits a stop token and free-runs to
# max_tokens (observed: Gemma 3 generating a fabricated multi-turn "System:/User:/
# Assistant:" transcript until the context limit, ~100s per turn). Pinning
# chat_format + explicit stop sequences per family prevents the runaway.

def resolve_local_model_family(model_id: Optional[str]) -> Optional[str]:
    """Map a local model id (e.g. ``gemma-3-4b``) to a coarse family key.

    Longest substring wins so distinct families are matched unambiguously.
    Returns ``None`` for unknown models (callers then fall back to auto-detect).
    """
    from .profiles import resolve_local_model_family as resolve_profile_family

    return resolve_profile_family(model_id)


def chat_format_for_model(model_id: Optional[str]) -> Optional[str]:
    """llama.cpp ``chat_format`` for a model, or ``None`` to let llama.cpp auto-detect."""
    from .profiles import profile_for_model

    return profile_for_model(model_id).chat_format


class ModelMetadata(BaseModel):
    """
    Metadata for a loaded model.
    
    Attributes:
        model_id: Unique identifier for the model
        size_gb: Model size in gigabytes
        loaded_at: Unix timestamp when model was loaded
        last_used: Unix timestamp of last usage
        load_time_seconds: Time taken to load the model
        engine: Inference engine used (vllm, llama_cpp, transformers)
        quantization: Quantization format if applicable (e.g., "int8", "int4")
    """
    model_id: str
    size_gb: float = Field(ge=0.0, description="Model size in GB")
    loaded_at: float = Field(description="Unix timestamp when loaded")
    last_used: float = Field(description="Unix timestamp of last use")
    load_time_seconds: float = Field(ge=0.0, description="Load time in seconds")
    engine: str = Field(description="Inference engine (vllm, llama_cpp, transformers)")
    quantization: Optional[str] = Field(None, description="Quantization format (int8, int4, etc.)")
    
    model_config = {"frozen": False}  # Allow updates to last_used


class LocalModelCache:
    """
    LRU cache for local models with automatic eviction.
    Supports CPU, NVIDIA GPU, and Apple Silicon inference.
    
    Manages model loading, caching, and eviction based on memory constraints
    (GPU VRAM or system RAM). Supports multiple inference engines with automatic selection.
    """
    
    def __init__(
        self,
        device_id: int = 0,
        max_memory_gb: float = 24.0,
        cache_size: int = 3,
        engine: str = "auto"
    ):
        """
        Initialize local model cache.
        
        Args:
            device_id: Device ID (for NVIDIA GPUs, ignored for CPU/Apple Silicon)
            max_memory_gb: Maximum memory to use in GB (GPU VRAM or system RAM)
            cache_size: Maximum number of models to cache
            engine: Inference engine ("auto", "vllm", "llama_cpp", "transformers")
        """
        self.device_id = device_id
        self.max_memory = max_memory_gb
        self.cache_size = cache_size
        self.current_memory_gb = 0.0
        
        # Auto-detect best engine if not specified
        if engine == "auto":
            engine = self._detect_best_engine()
        self.engine = engine
        
        # LRU cache: OrderedDict maintains insertion order
        self.loaded_models: OrderedDict[str, Any] = OrderedDict()
        self.model_metadata: Dict[str, ModelMetadata] = {}
        
        # Thread-safe access (works on all pool types)
        self.lock = WorkerLock()
        
        logger.info(
            "Initialized local model cache",
            device_id=device_id,
            max_memory_gb=max_memory_gb,
            cache_size=cache_size,
            engine=engine
        )
    
    def _detect_best_engine(self) -> str:
        """Auto-detect best inference engine based on platform"""
        system = platform.system()
        
        if system == "Darwin":
            # Apple Silicon - llama.cpp with Metal
            logger.info("Auto-selected llama.cpp engine for Apple Silicon")
            return "llama_cpp"
        else:
            # Try vLLM for NVIDIA (best performance)
            try:
                import pynvml  # type: ignore[reportMissingImports]
                pynvml.nvmlInit()
                device_count = pynvml.nvmlDeviceGetCount()
                pynvml.nvmlShutdown()
                
                if device_count > 0:
                    logger.info("Auto-selected vLLM engine for NVIDIA GPU")
                    return "vllm"
            except Exception:
                pass  # GPU detection optional; fallback to llama.cpp
            
            # Fallback to llama.cpp (works on CPU too)
            logger.info("Auto-selected llama.cpp engine (fallback)")
            return "llama_cpp"
    
    def get_or_load_model_sync(
        self,
        model_id: str,
        **kwargs
    ) -> Tuple[Any, ModelMetadata]:
        """
        Get model from cache or load it (synchronous).
        
        Args:
            model_id: Model identifier (HuggingFace ID or local path)
            **kwargs: Additional loading parameters (quantization, etc.)
        
        Returns:
            Tuple of (model, metadata)
        """
        with self.lock:
            # Check if model is already loaded
            if model_id in self.loaded_models:
                # Move to end (most recently used)
                self.loaded_models.move_to_end(model_id)
                self.model_metadata[model_id].last_used = time.time()
                
                logger.debug(
                    "Model cache hit",
                    model_id=model_id,
                    cached_models=list(self.loaded_models.keys())
                )
                
                return self.loaded_models[model_id], self.model_metadata[model_id]
            
            # Model not in cache - need to load
            logger.info(
                "Model cache miss, loading model",
                model_id=model_id,
                engine=self.engine
            )
            
            # Check if we need to evict models
            model_size_estimate = self._estimate_model_size(model_id, **kwargs)
            self._ensure_memory_available(model_size_estimate)
            
            # Load the model
            start_time = time.time()
            model, actual_size = self._load_model(model_id, **kwargs)
            load_time = time.time() - start_time
            
            # Update cache
            self.loaded_models[model_id] = model
            self.model_metadata[model_id] = ModelMetadata(
                model_id=model_id,
                size_gb=actual_size,
                loaded_at=time.time(),
                last_used=time.time(),
                load_time_seconds=load_time,
                engine=self.engine,
                quantization=kwargs.get('quantization')
            )
            self.current_memory_gb += actual_size
            
            logger.info(
                "Model loaded successfully",
                model_id=model_id,
                size_gb=round(actual_size, 2),
                load_time_seconds=round(load_time, 2),
                current_memory_gb=round(self.current_memory_gb, 2)
            )
            
            return model, self.model_metadata[model_id]
    
    def _ensure_memory_available(self, required_gb: float):
        """Evict models if necessary to make room for new model"""
        while (self.current_memory_gb + required_gb > self.max_memory or
               len(self.loaded_models) >= self.cache_size):
            
            if not self.loaded_models:
                break  # No models to evict
            
            # Evict least recently used model (first in OrderedDict)
            lru_model_id = next(iter(self.loaded_models))
            self._evict_model(lru_model_id)
    
    def _evict_model(self, model_id: str):
        """Evict a model from cache"""
        if model_id not in self.loaded_models:
            return
        
        metadata = self.model_metadata[model_id]
        
        logger.info(
            "Evicting model from cache (LRU)",
            model_id=model_id,
            size_gb=round(metadata.size_gb, 2),
            age_seconds=round(time.time() - metadata.loaded_at, 1)
        )
        
        # Remove from cache
        model = self.loaded_models.pop(model_id)
        del self.model_metadata[model_id]
        self.current_memory_gb -= metadata.size_gb
        
        # Unload model from memory (GPU VRAM or system RAM)
        self._unload_model(model, model_id)
    
    def _load_model(self, model_id: str, **kwargs) -> Tuple[Any, float]:
        """Load model using specified engine"""
        if self.engine == "vllm":
            return self._load_vllm_model(model_id, **kwargs)
        elif self.engine == "llama_cpp":
            return self._load_llama_cpp_model(model_id, **kwargs)
        elif self.engine == "transformers":
            return self._load_transformers_model(model_id, **kwargs)
        else:
            raise ValueError(f"Unknown inference engine: {self.engine}")
    
    def _load_vllm_model(self, model_id: str, **kwargs) -> Tuple[Any, float]:
        """Load model using vLLM engine"""
        try:
            from vllm import LLM  # type: ignore[import-not-found]
            
            logger.info("Loading model with vLLM", model_id=model_id)
            
            # vLLM automatically handles GPU memory management
            model = LLM(
                model=model_id,
                tensor_parallel_size=1,  # Single GPU
                gpu_memory_utilization=0.9,  # Use up to 90% of GPU memory
                quantization=kwargs.get('quantization'),  # "awq", "gptq", etc.
                dtype="auto",
                trust_remote_code=kwargs.get('trust_remote_code', False),
                max_model_len=kwargs.get('max_model_len')
            )
            
            # Estimate size from GPU memory usage
            try:
                from motet.core.workers.hardware_detection import get_gpu_info
                gpu_info = get_gpu_info()
                # Rough estimate based on total GPU memory
                size_gb = gpu_info.get('total_memory_gb', 24) * 0.8
            except Exception:
                size_gb = self._estimate_model_size(model_id, **kwargs)
            
            return model, size_gb
            
        except ImportError as e:
            logger.error("vLLM not installed", error=str(e))
            raise RuntimeError("vLLM not installed. Install with: pip install vllm") from e
        except Exception as e:
            logger.error("Failed to load model with vLLM", model_id=model_id, error=str(e), exc_info=True)
            raise
    
    @staticmethod
    def _apply_chat_template_fallback(
        model: Any, model_id: str, fallback_chat_format: Optional[str]
    ) -> None:
        """Keep the embedded Jinja template if present; else pin the family handler.

        ADR-0117: the model is loaded with ``chat_format`` unset so llama.cpp uses
        the GGUF's embedded Jinja chat template when its metadata carries one. If
        the GGUF has no usable ``tokenizer.chat_template``, llama.cpp silently
        falls back to a Llama-2 handler (the ADR-0114 runaway cause); in that case
        we override with the pinned per-family ``chat_format`` instead.
        """
        has_embedded_template = False
        try:
            metadata = getattr(model, "metadata", None) or {}
            has_embedded_template = bool(metadata.get("tokenizer.chat_template"))
        except Exception:
            has_embedded_template = False

        if has_embedded_template:
            logger.info("local.chat_template.embedded_jinja", model_id=model_id)
            return

        if fallback_chat_format:
            try:
                model.chat_format = fallback_chat_format
                model.chat_handler = None
                logger.info(
                    "local.chat_template.pinned_fallback",
                    model_id=model_id,
                    chat_format=fallback_chat_format,
                )
            except Exception as e:
                logger.warning(
                    "local.chat_template.fallback_failed",
                    model_id=model_id,
                    chat_format=fallback_chat_format,
                    error=str(e),
                )
        else:
            logger.warning("local.chat_template.unknown", model_id=model_id)

    def _build_vision_chat_handler(self, model_id: str, mmproj_path: str) -> Optional[Any]:
        """Build a llama.cpp multimodal chat handler for a vision GGUF (ADR-0064 Phase 3).

        Selects a family-appropriate chat handler that loads the ``mmproj`` CLIP
        projector so image content blocks are accepted. Returns ``None`` (graceful
        degradation to text) if the handler/asset is unavailable, so a missing or
        incompatible projector never blocks loading the text path.
        """
        if not mmproj_path or not os.path.exists(mmproj_path):
            logger.warning(
                "local.vision.mmproj_missing", model_id=model_id, mmproj_path=mmproj_path
            )
            return None
        try:
            from llama_cpp import llama_chat_format  # type: ignore[reportMissingImports]

            family = resolve_local_model_family(model_id)
            # Map known vision families to their llama.cpp chat handler. Extend as
            # vision GGUFs are added to the tier.
            handler_cls = {
                "gemma": getattr(llama_chat_format, "Gemma3ChatHandler", None),
                "qwen": getattr(llama_chat_format, "Qwen25VLChatHandler", None),
            }.get(family or "")
            if handler_cls is None:
                # Fall back to the widely-compatible Llava handler.
                handler_cls = getattr(llama_chat_format, "Llava15ChatHandler", None)
            if handler_cls is None:
                logger.warning("local.vision.handler_unavailable", model_id=model_id)
                return None
            logger.info(
                "local.vision.mmproj_loaded",
                model_id=model_id,
                handler=handler_cls.__name__,
            )
            return handler_cls(clip_model_path=mmproj_path, verbose=False)
        except Exception as exc:  # pragma: no cover - depends on optional vision build
            logger.warning(
                "local.vision.handler_build_failed", model_id=model_id, error=str(exc)
            )
            return None

    def _load_llama_cpp_model(self, model_id: str, **kwargs) -> Tuple[Any, float]:
        """Load model using llama.cpp engine"""
        model_path = kwargs.get("model_path", model_id)
        try:
            from llama_cpp import Llama  # type: ignore[reportMissingImports]

            # ADR-0117: prefer the GGUF's embedded Jinja chat template (the model
            # author's canonical formatting, incl. thinking/tool-call tokens). Only
            # fall back to the pinned per-family chat_format (ADR-0114) when the GGUF
            # lacks usable template metadata, so a templateless GGUF can't silently
            # drop to llama.cpp's Llama-2 default and free-run. An explicit
            # chat_format kwarg still forces a specific handler. The end-of-turn
            # stop-sequence safety net (ADR-0114) is applied at inference time
            # regardless of which template path is active.
            forced_chat_format = kwargs.get('chat_format')
            fallback_chat_format = chat_format_for_model(model_id)

            logger.info(
                "Loading model with llama.cpp",
                model_path=model_path,
                chat_format=(
                    forced_chat_format
                    or f"embedded-jinja (fallback: {fallback_chat_format or 'auto'})"
                ),
            )

            resolved_n_ctx = kwargs.get('n_ctx') or _resolve_default_n_ctx()
            llama_kwargs: Dict[str, Any] = dict(
                model_path=model_path,
                n_gpu_layers=-1,  # Offload layers to accelerator (GPU/Metal/CPU)
                n_ctx=resolved_n_ctx,
                n_batch=kwargs.get('n_batch', 512),
                verbose=False,
            )
            logger.info("local.llama_cpp.n_ctx", model_id=model_id, n_ctx=resolved_n_ctx)
            # When not forced, leave chat_format unset so llama.cpp loads the
            # embedded Jinja template from GGUF metadata if present.
            if forced_chat_format:
                llama_kwargs['chat_format'] = forced_chat_format

            # Vision (ADR-0064 Phase 3): a vision-capable GGUF needs a multimodal
            # projector (mmproj) plus a chat handler that accepts image content
            # blocks. Activated only when an ``mmproj_path`` is supplied (no current
            # ADR-0117 tier model ships one, so this stays inert until a vision GGUF
            # + projector asset is added). The adapter maps canonical MediaParts to
            # OpenAI-style image_url blocks, gated on CAP_VISION.
            mmproj_path = kwargs.get('mmproj_path')
            if mmproj_path:
                chat_handler = self._build_vision_chat_handler(model_id, mmproj_path)
                if chat_handler is not None:
                    llama_kwargs['chat_handler'] = chat_handler
                    llama_kwargs.pop('chat_format', None)

            model = Llama(**llama_kwargs)

            if not forced_chat_format:
                self._apply_chat_template_fallback(
                    model, model_id, fallback_chat_format
                )
            
            # Estimate model size from file size
            try:
                size_bytes = os.path.getsize(model_path)
                size_gb = size_bytes / (1024 ** 3)
            except Exception:
                size_gb = self._estimate_model_size(model_id, **kwargs)
            
            return model, size_gb
            
        except ImportError as e:
            logger.error("llama-cpp-python not installed", error=str(e))
            raise RuntimeError("llama-cpp-python not installed. Install with: pip install llama-cpp-python") from e
        except Exception as e:
            logger.error("Failed to load model with llama.cpp", model_path=model_path, error=str(e), exc_info=True)
            raise
    
    def _load_transformers_model(self, model_id: str, **kwargs) -> Tuple[Any, float]:
        """Load model using transformers library"""
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer
            
            logger.info("Loading model with transformers", model_id=model_id)
            
            # Determine device
            if platform.system() == "Darwin":
                # Apple Silicon - use MPS if available
                device_map = "mps" if torch.backends.mps.is_available() else "cpu"
            else:
                # NVIDIA - use CUDA
                device_map = f"cuda:{self.device_id}"
            
            # Load model
            model = AutoModelForCausalLM.from_pretrained(
                model_id,
                device_map=device_map,
                torch_dtype=torch.float16 if device_map != "cpu" else torch.float32,
                trust_remote_code=kwargs.get('trust_remote_code', False),
                load_in_8bit=kwargs.get('quantization') == 'int8',
                load_in_4bit=kwargs.get('quantization') == 'int4'
            )
            
            # Load tokenizer
            tokenizer = AutoTokenizer.from_pretrained(model_id)
            
            # Estimate size
            param_count = sum(p.numel() for p in model.parameters())
            size_gb = (param_count * 2) / (1024 ** 3)  # Assuming FP16
            
            if kwargs.get('quantization') == 'int8':
                size_gb *= 0.5
            elif kwargs.get('quantization') == 'int4':
                size_gb *= 0.25
            
            return (model, tokenizer), size_gb
            
        except ImportError as e:
            logger.error("transformers not installed", error=str(e))
            raise RuntimeError("transformers not installed. Install with: pip install transformers") from e
        except Exception as e:
            logger.error("Failed to load model with transformers", model_id=model_id, error=str(e), exc_info=True)
            raise
    
    def _unload_model(self, model: Any, model_id: str):
        """Unload model from memory (GPU VRAM or system RAM)"""
        try:
            # Engine-specific cleanup
            if self.engine == "vllm":
                # vLLM handles cleanup automatically
                del model
            elif self.engine == "llama_cpp":
                # llama.cpp cleanup
                del model
            elif self.engine == "transformers":
                # PyTorch cleanup
                model_obj, tokenizer = model
                del model_obj, tokenizer
                
                # Clear CUDA cache if available
                try:
                    import torch
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                except Exception:
                    pass  # CUDA cleanup optional; torch may be unavailable
            
            # Force garbage collection
            gc.collect()
            
            logger.debug("Model unloaded", model_id=model_id)
            
        except Exception as e:
            logger.warning("Error unloading model", model_id=model_id, error=str(e))
    
    def _estimate_model_size(self, model_id: str, **kwargs) -> float:
        """Estimate model size in GB for memory planning"""
        # Rough estimates based on parameter count (FP16; adjusted for quantization
        # below). Ordered most-specific-first so multi-size names resolve correctly
        # (e.g. the MoE "gemma-4-26b-a4b" matches "26b" before the "a4b"→"4b" key).
        size_estimates = {
            "70b": 140, # 70B params in FP16 = ~140GB
            "30b": 60,  # 30B params in FP16 = ~60GB
            "26b": 52,  # 26B params (e.g. Gemma 4 26B-A4B MoE; all params resident)
            "13b": 26,  # 13B params in FP16 = ~26GB
            "8b": 16,   # 8B params in FP16 = ~16GB
            "7b": 14,   # 7B params in FP16 = ~14GB
            "4b": 8,    # ~4B params (e.g. Gemma 4 E4B, Gemma 3 4B) in FP16 = ~8GB
        }
        
        model_lower = model_id.lower()
        for size_key, size_gb in size_estimates.items():
            if size_key in model_lower:
                # Adjust for quantization
                quantization = kwargs.get('quantization')
                if quantization == 'int8':
                    return size_gb * 0.5  # 50% of FP16 size
                elif quantization == 'int4':
                    return size_gb * 0.25  # 25% of FP16 size
                return size_gb
        
        # Default conservative estimate
        return 20.0
    
    def get_cache_stats(self) -> Dict[str, Any]:
        """Get cache statistics for monitoring"""
        return {
            'loaded_models': list(self.loaded_models.keys()),
            'model_count': len(self.loaded_models),
            'max_cache_size': self.cache_size,
            'current_memory_gb': round(self.current_memory_gb, 2),
            'max_memory_gb': self.max_memory,
            'memory_utilization': round(self.current_memory_gb / self.max_memory, 2) if self.max_memory > 0 else 0,
            'engine': self.engine,
            'models_metadata': {
                model_id: {
                    'size_gb': round(meta.size_gb, 2),
                    'load_time_seconds': round(meta.load_time_seconds, 2),
                    'last_used_seconds_ago': round(time.time() - meta.last_used, 1),
                    'engine': meta.engine,
                    'quantization': meta.quantization
                }
                for model_id, meta in self.model_metadata.items()
            }
        }
    
    def clear_cache(self):
        """Clear all models from cache"""
        with self.lock:
            logger.info("Clearing local model cache", model_count=len(self.loaded_models))
            
            # Evict all models
            for model_id in list(self.loaded_models.keys()):
                self._evict_model(model_id)
            
            self.current_memory_gb = 0.0

