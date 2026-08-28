"""
Motet - Edge MCP Config Filter Unit Tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-09

Description:
    Unit tests for the edge MCP config filtering utility used by edge worker
    docker startup.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from motet.core.edge.mcp_config_filter import filter_mcp_config


def test_filter_mcp_config_writes_selected_services(tmp_path: Path) -> None:
    source = tmp_path / "mcp.yaml"
    out = tmp_path / "filtered.yaml"
    source.write_text(
        yaml.safe_dump(
            {
                "services": [
                    {"service_id": "google_workspace", "transport": "stdio"},
                    {"service_id": "playwright", "transport": "stdio"},
                ]
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    rc = filter_mcp_config(
        config_path=source,
        selected_services={"playwright"},
        output_path=out,
    )
    assert rc == 0
    assert out.exists()
    data = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert [row["service_id"] for row in data["services"]] == ["playwright"]


def test_filter_mcp_config_no_services_is_noop(tmp_path: Path) -> None:
    source = tmp_path / "mcp.yaml"
    out = tmp_path / "filtered.yaml"
    source.write_text("services: []\n", encoding="utf-8")

    rc = filter_mcp_config(
        config_path=source,
        selected_services=set(),
        output_path=out,
    )
    assert rc == 0
    assert not out.exists()
