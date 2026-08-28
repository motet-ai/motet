"""
Motet - Distributed Metrics Collector

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-26

Description:
    Redis-based distributed metrics collector. Workers push samples to Redis;
    the HTTP server reads them for ``/metrics``. Listing uses
    ``SMEMBERS worker:metrics:index`` then ``GET`` per sample (not a
    keyspace scan).

Dependencies:
    - pydantic: Metric sample validation
    - json: Metric payload serialization
    - time: Sample timestamps
    - Unified Redis manager for worker-side sync writes

Usage:
    from motet.core.observability.distributed_metrics import (
        DistributedMetricsCollector,
        MetricSample,
    )

    collector = DistributedMetricsCollector(redis_client)
    collector.push_metric_sync(
        "worker1",
        MetricSample(name="cpu_usage", value=75.5, labels={"worker": "worker1"}, timestamp=0.0),
    )
    metrics = await collector.get_all_metrics()

Notes:
    - Samples are ``SETEX worker:metrics:{worker_id}:{name}`` with a 5 minute TTL
    - ``worker:metrics:index`` holds ``{worker_id}:{name}`` members
    - Empty index means no samples until the next push
    - Missing samples (TTL expiry) are skipped and dropped from the set
"""


import json
import time
from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class MetricSample(BaseModel):
    """A single metric sample with timestamp and labels."""
    name: str
    value: float
    labels: Dict[str, str]
    timestamp: float
    help_text: str = ""
    metric_type: str = "gauge"  # gauge, counter, histogram


class DistributedMetricsCollector:
    """
    Redis-based metrics collector for aggregating metrics from distributed workers.
    
    Workers push metrics to Redis, and the HTTP server pulls them for the /metrics endpoint.
    """
    
    def __init__(self, redis_client: Any, key_prefix: str = "worker:metrics"):
        self.redis = redis_client
        self.key_prefix = key_prefix
        self.ttl_seconds = 300  # 5 minutes TTL for metrics
    
    def _make_key(self, worker_id: str, metric_name: str) -> str:
        """Generate Redis key for worker metric."""
        return f"{self.key_prefix}:{worker_id}:{metric_name}"
    
    def _make_index_key(self) -> str:
        """Generate Redis key for metrics index."""
        return f"{self.key_prefix}:index"

    def _decode_member(self, member: Any) -> str:
        """Normalize a Redis set member to a worker:metric suffix."""
        if isinstance(member, bytes):
            return member.decode("utf-8")
        return str(member)
    
    def push_metric_sync(self, worker_id: str, metric: MetricSample) -> None:
        """Push a metric sample from a worker to the shared collector."""
        try:
            # Use unified Redis manager for consistent connection handling
            from ..distributed.redis_manager import get_sync_redis_client
            sync_redis = get_sync_redis_client("distributed_metrics")
            
            # Store the metric sample
            key = self._make_key(worker_id, metric.name)
            metric_data = metric.model_dump()
            metric_data['worker_id'] = worker_id
            
            # Store metric data with TTL
            sync_redis.setex(
                key,
                self.ttl_seconds,
                json.dumps(metric_data)
            )
            
            # Add to metrics index
            index_key = self._make_index_key()
            sync_redis.sadd(index_key, f"{worker_id}:{metric.name}")
            sync_redis.expire(index_key, self.ttl_seconds + 60)
            
        except Exception:
            # Don't let metrics collection break the application
            pass

    async def get_all_metrics(self) -> List[MetricSample]:
        """List samples from ``worker:metrics:index``. Empty index is empty."""
        if not self.redis:
            return []

        try:
            index_key = self._make_index_key()
            suffixes = [
                self._decode_member(member)
                for member in (await self.redis.smembers(index_key) or [])
            ]
            metrics: List[MetricSample] = []
            stale: List[str] = []
            for suffix in suffixes:
                raw = await self.redis.get(f"{self.key_prefix}:{suffix}")
                if not raw:
                    stale.append(suffix)
                    continue
                payload = raw.decode("utf-8") if isinstance(raw, bytes) else raw
                try:
                    data = json.loads(payload)
                    metrics.append(
                        MetricSample(
                            name=data["name"],
                            value=data["value"],
                            labels=data["labels"],
                            timestamp=data["timestamp"],
                            help_text=data.get("help_text", ""),
                            metric_type=data.get("metric_type", "gauge"),
                        )
                    )
                except Exception:
                    continue

            if stale:
                await self.redis.srem(index_key, *stale)
            return metrics
        except Exception:
            return []


def format_prometheus_metrics(metrics: List[MetricSample]) -> str:
    """
    Format metrics in Prometheus exposition format.
    
    Groups metrics by name and outputs proper Prometheus format with HELP and TYPE.
    For histogram metrics, generates proper buckets, count, and sum.
    """
    if not metrics:
        return ""
    
    # Group metrics by name and label combination
    metrics_by_key = {}
    for metric in metrics:
        # Create a key that combines metric name and labels (excluding worker_id for aggregation)
        labels_without_worker = {k: v for k, v in metric.labels.items() if k != 'worker_id'}
        key = (metric.name, tuple(sorted(labels_without_worker.items())))
        
        if key not in metrics_by_key:
            metrics_by_key[key] = []
        metrics_by_key[key].append(metric)
    
    lines = []
    processed_metrics = set()
    
    for (metric_name, label_items), metric_samples in metrics_by_key.items():
        if metric_name in processed_metrics:
            continue
        
        # Add HELP and TYPE lines (use first sample for metadata)
        first_sample = metric_samples[0]
        if first_sample.help_text:
            lines.append(f"# HELP {metric_name} {first_sample.help_text}")
        lines.append(f"# TYPE {metric_name} {first_sample.metric_type}")
        processed_metrics.add(metric_name)
        
        # Handle different metric types
        if first_sample.metric_type == "counter":
            # For counters, sum all values for each label combination
            for (current_metric_name, current_label_items), current_samples in metrics_by_key.items():
                if current_metric_name != metric_name:
                    continue
                    
                # Sum all counter values for this label combination
                total = sum(sample.value for sample in current_samples)
                labels_dict = dict(current_label_items)
                
                if labels_dict:
                    label_str = ",".join(f'{k}="{v}"' for k, v in labels_dict.items())
                    lines.append(f"{metric_name}{{{label_str}}} {total}")
                else:
                    lines.append(f"{metric_name} {total}")
                    
        elif first_sample.metric_type == "histogram":
            # Define histogram buckets
            buckets = [0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1.0, 2.5, 5.0, 7.5, 10.0, float('inf')]
            
            # Group by label combination (excluding worker_id)
            for (current_metric_name, current_label_items), current_samples in metrics_by_key.items():
                if current_metric_name != metric_name:
                    continue
                    
                # Aggregate all values for this label combination
                values = [sample.value for sample in current_samples]
                labels_dict = dict(current_label_items)
                
                # Generate bucket metrics
                for bucket in buckets:
                    count = sum(1 for v in values if v <= bucket)
                    bucket_label = "+Inf" if bucket == float('inf') else str(bucket)
                    
                    if labels_dict:
                        label_str = ",".join(f'{k}="{v}"' for k, v in labels_dict.items())
                        lines.append(f"{metric_name}_bucket{{le=\"{bucket_label}\",{label_str}}} {count}")
                    else:
                        lines.append(f"{metric_name}_bucket{{le=\"{bucket_label}\"}} {count}")
                
                # Generate count and sum metrics
                count = len(values)
                total = sum(values)
                
                if labels_dict:
                    label_str = ",".join(f'{k}="{v}"' for k, v in labels_dict.items())
                    lines.append(f"{metric_name}_count{{{label_str}}} {count}")
                    lines.append(f"{metric_name}_sum{{{label_str}}} {total}")
                else:
                    lines.append(f"{metric_name}_count {count}")
                    lines.append(f"{metric_name}_sum {total}")
        else:
            # Handle non-histogram metrics normally
            for sample in metric_samples:
                if sample.labels:
                    label_str = ",".join(f'{k}="{v}"' for k, v in sample.labels.items())
                    lines.append(f"{metric_name}{{{label_str}}} {sample.value}")
                else:
                    lines.append(f"{metric_name} {sample.value}")
    
    return "\n".join(lines) + "\n"


# Global collector instance
_global_collector: Optional[DistributedMetricsCollector] = None


def initialize_distributed_metrics(redis_client: Any) -> None:
    """Initialize the global distributed metrics collector."""
    global _global_collector
    _global_collector = DistributedMetricsCollector(redis_client)


def get_distributed_metrics_collector() -> Optional[DistributedMetricsCollector]:
    """Get the global distributed metrics collector."""
    return _global_collector


def push_tool_latency_metric(worker_id: str, tool_name: str, latency_seconds: float) -> None:
    """Push a tool latency metric from a worker (sync)."""
    if not _global_collector:
        return
    
    metric = MetricSample(
        name="imf_tool_latency_seconds",
        value=latency_seconds,
        labels={"tool": tool_name, "worker_id": worker_id},
        timestamp=time.time(),
        help_text="Tool execution latency",
        metric_type="histogram"
    )
    
    _global_collector.push_metric_sync(worker_id, metric)


def push_tool_request_metric(worker_id: str, tool_name: str) -> None:
    """Push a tool request counter metric from a worker (sync)."""
    if not _global_collector:
        return
    
    metric = MetricSample(
        name="imf_tool_requests_total",
        value=1,
        labels={"tool": tool_name, "worker_id": worker_id},
        timestamp=time.time(),
        help_text="Tool requests by tool",
        metric_type="counter"
    )
    
    _global_collector.push_metric_sync(worker_id, metric)


def push_model_latency_metric(worker_id: str, provider: str, model: str, latency_seconds: float) -> None:
    """Push a model latency metric from a worker (sync)."""
    if not _global_collector:
        return
    
    metric = MetricSample(
        name="imf_model_latency_seconds",
        value=latency_seconds,
        labels={"provider": provider, "model": model, "worker_id": worker_id},
        timestamp=time.time(),
        help_text="Model completion latency",
        metric_type="histogram"
    )
    
    _global_collector.push_metric_sync(worker_id, metric)
