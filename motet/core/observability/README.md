## Package: observability

**Distributed observability infrastructure** with comprehensive logging, metrics, and tracing for distributed AI operations.

### Purpose
- **Distributed Logging**: Structured logging across distributed workers with correlation
- **Comprehensive Metrics**: Prometheus metrics for distributed commands, workers, and system health
- **Distributed Tracing**: Trace distributed command execution across multiple workers
- **Performance Monitoring**: Monitor distributed system performance and bottlenecks

### Components
- `logging.py`: Logger initialization.
- `metrics.py`: Prometheus counters/histograms/gauges (tools, models, circuits).
- `distributed_metrics.py`: workers push samples; `/metrics` lists `SMEMBERS worker:metrics:index` then `GET` each sample.
- `trace_store.py`: JSONL/Redis trace storage and retrieval APIs.
- `tracing.py`: Lightweight tracer/get_tracer helper.

### Implemented
- Structured logging via structlog with contextvars (`trace_id`).
- Request, tool, model, scheduler metrics; circuit breaker transitions/blocked.
- Trace storage to JSONL and Redis; API and CLI to list/show.

### Planned
- Distributed tracing (OpenTelemetry) integration and exporter config.
- Request correlation across components; tracing for event bus/durable path.
- Grafana dashboards expansion and alert rules per SLOs.

