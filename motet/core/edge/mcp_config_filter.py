"""
Motet - Edge MCP Config Filter

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    CLI utility to filter `mcp_instance_manager.yaml` services by service_id for
    edge worker startup flows. This replaces shell heredoc-based inline Python
    snippets in docker-compose command blocks.

Dependencies:
    - argparse: CLI argument parsing
    - pathlib: File path handling
    - yaml: YAML read/write

Usage:
    python -m motet.core.edge.mcp_config_filter \
        --config /app/config/mcp_instance_manager.yaml \
        --services google_workspace,playwright \
        --output /tmp/mcp_instance_manager.edge.filtered.yaml

Notes:
    - If `--services` is empty, no file is written and command exits successfully.
    - Output YAML preserves key ordering via `sort_keys=False`.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Dict, List, Set

import yaml


def _parse_services(raw: str) -> Set[str]:
    return {item.strip() for item in (raw or "").split(",") if item.strip()}


def filter_mcp_config(*, config_path: Path, selected_services: Set[str], output_path: Path) -> int:
    """
    Filter MCP config services by `selected_services` and write output YAML.

    Returns:
        0 on success.
    """
    if not selected_services:
        return 0

    if not config_path.exists():
        raise FileNotFoundError(f"MCP config not found: {config_path}")

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise RuntimeError(f"Invalid MCP config root object: {config_path}")

    services = raw.get("services")
    if not isinstance(services, list):
        raise RuntimeError(f"Invalid MCP config (missing services list): {config_path}")

    filtered: List[Dict[str, Any]] = []
    for service in services:
        if not isinstance(service, dict):
            continue
        service_id = str(service.get("service_id") or "").strip()
        if service_id in selected_services:
            filtered.append(service)
    raw["services"] = filtered

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Filter local MCP service config by service_id.")
    parser.add_argument("--config", required=True, help="Path to source mcp_instance_manager.yaml")
    parser.add_argument("--services", default="", help="Comma-separated service_id filter")
    parser.add_argument("--output", required=True, help="Path to write filtered YAML")
    args = parser.parse_args()

    return filter_mcp_config(
        config_path=Path(args.config),
        selected_services=_parse_services(args.services),
        output_path=Path(args.output),
    )


if __name__ == "__main__":
    raise SystemExit(main())
