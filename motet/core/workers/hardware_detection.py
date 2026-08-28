"""
Motet - Hardware Detection

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Cross-platform hardware detection for CPU, NVIDIA GPUs, and Apple Silicon.
    Detects compute capabilities, GPU presence, and system information for
    local model inference.
    
    Supports:
    - NVIDIA GPUs via pynvml (CUDA)
    - Apple Silicon (M1/M2/M3/M4) via system detection
    - CPU-only systems (graceful degradation)

Dependencies:
    - pynvml: NVIDIA GPU management (optional, for NVIDIA GPUs)
    - platform: System platform detection

Usage:
    from motet.core.workers.hardware_detection import has_gpu, get_gpu_info
    
    if has_gpu():
        gpu_info = get_gpu_info()
        print(f"GPU: {gpu_info['gpu_name']}")
        print(f"Memory: {gpu_info['total_memory_gb']} GB")

Notes:
    - Returns False/empty if no GPU detected (not an error)
    - Apple Silicon detection via platform.machine() check
    - NVIDIA detection via pynvml library
    - Gracefully handles missing pynvml on non-NVIDIA systems
"""

import os
import platform
import subprocess
from typing import Any, Dict, Optional, cast
import structlog

logger = structlog.get_logger(__name__)


def has_gpu() -> bool:
    """
    Check if GPU is available on this system.
    
    Returns:
        True if NVIDIA GPU or Apple Silicon GPU detected, False otherwise.
    """
    system = platform.system()
    
    if system == "Darwin":
        # macOS - check for Apple Silicon
        machine = platform.machine()
        has_apple_gpu = machine in ["arm64", "aarch64"]
        logger.debug(
            "Apple Silicon detection",
            has_gpu=has_apple_gpu,
            machine=machine,
            system=system
        )
        return has_apple_gpu
    else:
        # Linux/Windows - check for NVIDIA GPU
        try:
            import pynvml
            pynvml.nvmlInit()
            device_count = pynvml.nvmlDeviceGetCount()
            pynvml.nvmlShutdown()
            has_nvidia = device_count > 0
            logger.debug(
                "NVIDIA GPU detection",
                has_gpu=has_nvidia,
                device_count=device_count,
                system=system
            )
            return has_nvidia
        except Exception as e:
            logger.debug(
                "No NVIDIA GPU detected",
                error=str(e),
                system=system
            )
            return False


def get_gpu_info() -> Dict[str, Any]:
    """
    Get detailed GPU information.
    
    Returns:
        Dictionary with GPU information:
        - has_gpu: bool
        - gpu_type: "nvidia" or "apple_silicon" or "none"
        - gpu_name: str (device name)
        - total_memory_gb: float
        - gpu_id: int (for NVIDIA, 0 for Apple Silicon)
        - driver_version: str (NVIDIA) or platform info (Apple)
        - cuda_version: str (NVIDIA only)
        - compute_capability: str (NVIDIA only)
    """
    system = platform.system()
    
    if system == "Darwin":
        return _get_apple_silicon_info()
    else:
        return _get_nvidia_gpu_info()


def _get_apple_silicon_info() -> Dict[str, Any]:
    """Get Apple Silicon GPU information."""
    machine = platform.machine()
    
    if machine not in ["arm64", "aarch64"]:
        return {
            'has_gpu': False,
            'gpu_type': 'none',
            'gpu_name': 'None',
            'total_memory_gb': 0.0,
            'gpu_id': -1
        }
    
    # Get chip model via sysctl
    try:
        result = subprocess.run(
            ['sysctl', '-n', 'machdep.cpu.brand_string'],
            capture_output=True,
            text=True,
            timeout=5
        )
        chip_name = result.stdout.strip()
    except Exception as e:
        logger.warning("Failed to get chip name", error=str(e))
        chip_name = "Apple Silicon"
    
    # Get total memory (unified memory on Apple Silicon)
    try:
        result = subprocess.run(
            ['sysctl', '-n', 'hw.memsize'],
            capture_output=True,
            text=True,
            timeout=5
        )
        memory_bytes = int(result.stdout.strip())
        memory_gb = memory_bytes / (1024 ** 3)
    except Exception as e:
        logger.warning("Failed to get memory size", error=str(e))
        memory_gb = 16.0  # Conservative default
    
    gpu_info = {
        'has_gpu': True,
        'gpu_type': 'apple_silicon',
        'gpu_name': chip_name,
        'total_memory_gb': round(memory_gb, 1),
        'gpu_id': 0,  # Apple Silicon has integrated GPU
        'driver_version': platform.mac_ver()[0],  # macOS version
        'platform': 'darwin',
        'architecture': machine,
        'unified_memory': True  # CPU and GPU share memory
    }
    
    logger.info(
        "Apple Silicon GPU detected",
        gpu_name=gpu_info['gpu_name'],
        memory_gb=gpu_info['total_memory_gb']
    )
    
    return gpu_info


def _get_nvidia_gpu_info() -> Dict[str, Any]:
    """Get NVIDIA GPU information."""
    try:
        import pynvml
        
        pynvml.nvmlInit()
        device_count = pynvml.nvmlDeviceGetCount()
        
        if device_count == 0:
            pynvml.nvmlShutdown()
            return {
                'has_gpu': False,
                'gpu_type': 'none',
                'gpu_name': 'None',
                'total_memory_gb': 0.0,
                'gpu_id': -1
            }
        
        # Get info for first GPU (can be extended for multi-GPU)
        gpu_id = int(os.getenv('CUDA_VISIBLE_DEVICES', '0').split(',')[0])
        if gpu_id >= device_count:
            gpu_id = 0
        
        handle = pynvml.nvmlDeviceGetHandleByIndex(gpu_id)
        
        # Get GPU name
        gpu_name = pynvml.nvmlDeviceGetName(handle)
        if isinstance(gpu_name, bytes):
            gpu_name = gpu_name.decode('utf-8')
        
        # Get memory info (pynvml stubs may widen field types; normalize to int bytes)
        memory_info = pynvml.nvmlDeviceGetMemoryInfo(handle)
        mem_total = int(cast(Any, memory_info.total))
        memory_gb = mem_total / (1024 ** 3)
        
        # Get driver and CUDA version
        driver_version = pynvml.nvmlSystemGetDriverVersion()
        if isinstance(driver_version, bytes):
            driver_version = driver_version.decode('utf-8')
        
        cuda_version = pynvml.nvmlSystemGetCudaDriverVersion()
        cuda_version_str = f"{cuda_version // 1000}.{(cuda_version % 1000) // 10}"
        
        # Get compute capability
        major, minor = pynvml.nvmlDeviceGetCudaComputeCapability(handle)
        compute_capability = f"{major}.{minor}"
        
        pynvml.nvmlShutdown()
        
        gpu_info = {
            'has_gpu': True,
            'gpu_type': 'nvidia',
            'gpu_name': gpu_name,
            'total_memory_gb': round(memory_gb, 1),
            'gpu_id': gpu_id,
            'driver_version': driver_version,
            'cuda_version': cuda_version_str,
            'compute_capability': compute_capability,
            'platform': platform.system().lower(),
            'unified_memory': False  # NVIDIA has separate VRAM
        }
        
        logger.info(
            "NVIDIA GPU detected",
            gpu_name=gpu_info['gpu_name'],
            memory_gb=gpu_info['total_memory_gb'],
            cuda_version=cuda_version_str
        )
        
        return gpu_info
        
    except ImportError:
        logger.debug("pynvml not installed, no NVIDIA GPU support")
        return {
            'has_gpu': False,
            'gpu_type': 'none',
            'gpu_name': 'None',
            'total_memory_gb': 0.0,
            'gpu_id': -1,
            'error': 'pynvml not installed'
        }
    except Exception as e:
        logger.warning("Failed to get NVIDIA GPU info", error=str(e), exc_info=True)
        return {
            'has_gpu': False,
            'gpu_type': 'none',
            'gpu_name': 'None',
            'total_memory_gb': 0.0,
            'gpu_id': -1,
            'error': str(e)
        }


def get_gpu_utilization(gpu_id: int = 0) -> Dict[str, float]:
    """
    Get current GPU utilization metrics.
    
    Args:
        gpu_id: GPU device ID (default: 0)
    
    Returns:
        Dictionary with utilization metrics:
        - compute_percent: GPU compute utilization (0-100)
        - memory_percent: GPU memory utilization (0-100)
        - temperature_celsius: GPU temperature
        - power_watts: GPU power consumption (NVIDIA only)
    """
    system = platform.system()
    
    if system == "Darwin":
        # Apple Silicon - limited metrics available
        return {
            'compute_percent': 0.0,  # Not available via public API
            'memory_percent': 0.0,   # Not available via public API
            'temperature_celsius': 0.0,
            'power_watts': 0.0
        }
    else:
        # NVIDIA GPU
        try:
            import pynvml
            
            pynvml.nvmlInit()
            handle = pynvml.nvmlDeviceGetHandleByIndex(gpu_id)
            
            # Get utilization
            utilization = pynvml.nvmlDeviceGetUtilizationRates(handle)
            
            # Get memory info (pynvml stubs may widen field types; normalize to int bytes)
            memory_info = pynvml.nvmlDeviceGetMemoryInfo(handle)
            mem_total = int(cast(Any, memory_info.total))
            mem_used = int(cast(Any, memory_info.used))
            memory_percent = (mem_used / mem_total) * 100 if mem_total else 0.0
            
            # Get temperature
            temperature = pynvml.nvmlDeviceGetTemperature(
                handle,
                pynvml.NVML_TEMPERATURE_GPU
            )
            
            # Get power usage
            try:
                power = pynvml.nvmlDeviceGetPowerUsage(handle) / 1000  # mW to W
            except Exception:
                power = 0.0  # not all GPUs support power query
            
            pynvml.nvmlShutdown()
            
            return {
                'compute_percent': float(utilization.gpu),
                'memory_percent': float(memory_percent),
                'temperature_celsius': float(temperature),
                'power_watts': float(power)
            }
            
        except Exception as e:
            logger.warning(
                "Failed to get GPU utilization",
                gpu_id=gpu_id,
                error=str(e)
            )
            return {
                'compute_percent': 0.0,
                'memory_percent': 0.0,
                'temperature_celsius': 0.0,
                'power_watts': 0.0
            }

