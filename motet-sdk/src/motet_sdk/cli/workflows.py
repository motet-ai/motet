"""
Motet - Workflows CLI

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-07

Description:
    CLI for workflows — calls /api/v1/workflows.
    List/validate/register/unregister/execute templates, plus run control for
    checkpointed runs (list/get/pause/cancel/resume) matching the HTTP surface
    from #149 and the workflow builder design note.

Dependencies:
    - click: CLI framework
    - motet.cli._auth: get_api_headers
    - motet.cli._api: api_request, normalize_base_url

Usage:
    motet-cli workflows list
    motet-cli workflows validate --yaml-file wf.yaml
    motet-cli workflows register --yaml-file wf.yaml [--replace]
    motet-cli workflows unregister <workflow_id>
    motet-cli workflows execute --workflow-id <id> --workflow-name <name> --steps <steps.json or stdin>
    motet-cli workflows runs list [--status paused] [--limit 50]
    motet-cli workflows runs get <workflow_run_id>
    motet-cli workflows runs pause <workflow_run_id> [--reason ...]
    motet-cli workflows runs cancel <workflow_run_id> [--reason ...]
    motet-cli workflows runs resume <workflow_run_id> --kind operator
    motet-cli workflows runs resume <workflow_run_id> --kind handback_tools --observations '[{...}]'
"""

from __future__ import annotations

import json
import sys
from typing import Any, Dict, Optional

import click

from ._api import api_request, api_url_option, normalize_base_url
from ._auth import get_api_headers

API_TIMEOUT = 120
RESUME_TIMEOUT = 7200

RESUME_KINDS = (
    "handback_tools",
    "elicitation",
    "confirmation",
    "oauth",
    "operator",
)


def _parse_json_option(raw: Optional[str], *, label: str) -> Any:
    """Parse a JSON CLI option; empty/None stays None."""
    if raw is None or raw == "":
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise click.ClickException(f"Invalid JSON for --{label}: {e}") from e


@click.group("workflows")
def workflows_group() -> None:
    """Workflow management (API: /api/v1/workflows)."""
    pass


@workflows_group.command("list")
@api_url_option()
def list_workflows(api_url: str) -> None:
    """List registered workflow templates (GET /api/v1/workflows)."""
    base = normalize_base_url(api_url)
    r = api_request("GET", f"{base}/api/v1/workflows", headers=get_api_headers(), timeout=API_TIMEOUT)
    data = r.json()
    for w in data.get("registered_workflows", []):
        click.echo(f"  {w.get('workflow_id')}  {w.get('name')}  steps={w.get('step_count', 0)}")


def _load_definition_payload(
    *,
    yaml_file: Optional[str],
    yaml_text: Optional[str],
    workflow_json: Optional[str],
    workflow_id: Optional[str],
    replace: bool,
) -> Dict[str, Any]:
    """Build validate/register JSON body from CLI options."""
    payload: Dict[str, Any] = {"replace": replace}
    if workflow_id:
        payload["workflow_id"] = workflow_id

    sources = sum(1 for x in (yaml_file, yaml_text, workflow_json) if x)
    if sources != 1:
        raise click.ClickException(
            "Provide exactly one of --yaml-file, --yaml, or --workflow-json"
        )

    if yaml_file:
        if yaml_file == "-":
            payload["yaml"] = sys.stdin.read()
        else:
            with open(yaml_file, "r", encoding="utf-8") as fh:
                payload["yaml"] = fh.read()
    elif yaml_text is not None:
        payload["yaml"] = yaml_text
    else:
        assert workflow_json is not None
        raw = sys.stdin.read() if workflow_json == "-" else workflow_json
        try:
            payload["workflow"] = json.loads(raw)
        except json.JSONDecodeError as e:
            raise click.ClickException(f"Invalid JSON for --workflow-json: {e}") from e
    return payload


@workflows_group.command("validate")
@click.option("--yaml-file", help="Path to workflow YAML (or '-' for stdin)")
@click.option("--yaml", "yaml_text", help="Inline workflow YAML string")
@click.option("--workflow-json", help="Inline workflow JSON object (or '-' for stdin)")
@click.option("--workflow-id", help="Optional workflow_id override")
@api_url_option()
def validate_workflow_cmd(
    yaml_file: Optional[str],
    yaml_text: Optional[str],
    workflow_json: Optional[str],
    workflow_id: Optional[str],
    api_url: str,
) -> None:
    """Validate a workflow definition (POST /api/v1/workflows/validate)."""
    base = normalize_base_url(api_url)
    payload = _load_definition_payload(
        yaml_file=yaml_file,
        yaml_text=yaml_text,
        workflow_json=workflow_json,
        workflow_id=workflow_id,
        replace=False,
    )
    r = api_request(
        "POST",
        f"{base}/api/v1/workflows/validate",
        headers=get_api_headers(),
        json=payload,
        timeout=API_TIMEOUT,
    )
    click.echo(json.dumps(r.json(), indent=2))


@workflows_group.command("register")
@click.option("--yaml-file", help="Path to workflow YAML (or '-' for stdin)")
@click.option("--yaml", "yaml_text", help="Inline workflow YAML string")
@click.option("--workflow-json", help="Inline workflow JSON object (or '-' for stdin)")
@click.option("--workflow-id", help="Optional workflow_id override")
@click.option("--replace", is_flag=True, help="Overwrite an existing user.* workflow")
@api_url_option()
def register_workflow_cmd(
    yaml_file: Optional[str],
    yaml_text: Optional[str],
    workflow_json: Optional[str],
    workflow_id: Optional[str],
    replace: bool,
    api_url: str,
) -> None:
    """Register a workflow definition (POST /api/v1/workflows/register)."""
    base = normalize_base_url(api_url)
    payload = _load_definition_payload(
        yaml_file=yaml_file,
        yaml_text=yaml_text,
        workflow_json=workflow_json,
        workflow_id=workflow_id,
        replace=replace,
    )
    r = api_request(
        "POST",
        f"{base}/api/v1/workflows/register",
        headers=get_api_headers(),
        json=payload,
        timeout=API_TIMEOUT,
    )
    click.echo(json.dumps(r.json(), indent=2))


@workflows_group.command("unregister")
@click.argument("workflow_id")
@api_url_option()
def unregister_workflow_cmd(workflow_id: str, api_url: str) -> None:
    """Unregister a user.* workflow (DELETE /api/v1/workflows/{workflow_id})."""
    base = normalize_base_url(api_url)
    r = api_request(
        "DELETE",
        f"{base}/api/v1/workflows/{workflow_id}",
        headers=get_api_headers(),
        timeout=API_TIMEOUT,
    )
    click.echo(json.dumps(r.json(), indent=2))


@workflows_group.command("export")
@click.argument("workflow_id")
@api_url_option()
def export_workflow_cmd(workflow_id: str, api_url: str) -> None:
    """Export a user.* workflow as bundle YAML (GET .../export)."""
    base = normalize_base_url(api_url)
    r = api_request(
        "GET",
        f"{base}/api/v1/workflows/{workflow_id}/export",
        headers=get_api_headers(),
        timeout=API_TIMEOUT,
    )
    data = r.json()
    yaml_text = data.get("yaml")
    if isinstance(yaml_text, str):
        click.echo(yaml_text)
    else:
        click.echo(json.dumps(data, indent=2))


@workflows_group.command("execute")
@click.option("--workflow-id", required=True, help="Workflow ID")
@click.option("--workflow-name", required=True, help="Workflow name")
@click.option("--steps", required=True, help="JSON array of steps (or '-' for stdin)")
@click.option("--context", default="{}", help="JSON execution context")
@api_url_option()
def execute_workflow(
    workflow_id: str,
    workflow_name: str,
    steps: str,
    context: str,
    api_url: str,
) -> None:
    """Execute a workflow (POST /api/v1/workflows/execute)."""
    base = normalize_base_url(api_url)
    steps_data = steps
    if steps == "-":
        steps_data = sys.stdin.read()
    steps_list = json.loads(steps_data)
    payload = {
        "workflow_id": workflow_id,
        "workflow_name": workflow_name,
        "steps": steps_list,
        "context": json.loads(context),
    }
    r = api_request(
        "POST", f"{base}/api/v1/workflows/execute", headers=get_api_headers(), json=payload, timeout=API_TIMEOUT
    )
    click.echo(json.dumps(r.json(), indent=2))

# ---------------------------------------------------------------------------
# Run control (checkpointed / paused runs — issue #149)
# ---------------------------------------------------------------------------


@workflows_group.group("runs")
def runs_group() -> None:
    """Inspect and control checkpointed workflow runs (API: /api/v1/workflows/runs)."""
    pass


@runs_group.command("list")
@click.option(
    "--status",
    default="paused",
    show_default=True,
    help="Filter status (currently only 'paused' is supported by the API).",
)
@click.option("--limit", default=50, show_default=True, type=int, help="Max runs to return (1–200).")
@click.option("--offset", default=0, show_default=True, type=int, help="Pagination offset.")
@api_url_option()
def list_runs(status: str, limit: int, offset: int, api_url: str) -> None:
    """List paused workflow runs (GET /api/v1/workflows/runs)."""
    base = normalize_base_url(api_url)
    r = api_request(
        "GET",
        f"{base}/api/v1/workflows/runs",
        headers=get_api_headers(),
        params={"status": status, "limit": limit, "offset": offset},
        timeout=API_TIMEOUT,
    )
    data = r.json()
    runs = data.get("runs") or []
    click.echo(f"status={data.get('status', status)} count={data.get('count', len(runs))}")
    for run in runs:
        if not isinstance(run, dict):
            click.echo(f"  {run}")
            continue
        rid = run.get("workflow_run_id") or run.get("run_id") or "?"
        wid = run.get("workflow_id") or ""
        reason = run.get("suspend_reason") or ""
        click.echo(f"  {rid}  workflow={wid}  suspend_reason={reason}")


@runs_group.command("get")
@click.argument("workflow_run_id")
@api_url_option()
def get_run(workflow_run_id: str, api_url: str) -> None:
    """Get a workflow run summary (GET /api/v1/workflows/runs/{id})."""
    base = normalize_base_url(api_url)
    r = api_request(
        "GET",
        f"{base}/api/v1/workflows/runs/{workflow_run_id}",
        headers=get_api_headers(),
        timeout=API_TIMEOUT,
    )
    click.echo(json.dumps(r.json(), indent=2))


@runs_group.command("pause")
@click.argument("workflow_run_id")
@click.option("--reason", default=None, help="Optional operator reason.")
@api_url_option()
def pause_run(workflow_run_id: str, reason: Optional[str], api_url: str) -> None:
    """Operator-pause a run (POST /api/v1/workflows/runs/{id}/pause)."""
    base = normalize_base_url(api_url)
    payload: Dict[str, Any] = {}
    if reason:
        payload["reason"] = reason
    r = api_request(
        "POST",
        f"{base}/api/v1/workflows/runs/{workflow_run_id}/pause",
        headers=get_api_headers(),
        json=payload,
        timeout=API_TIMEOUT,
    )
    click.echo(json.dumps(r.json(), indent=2))


@runs_group.command("cancel")
@click.argument("workflow_run_id")
@click.option("--reason", default=None, help="Optional operator reason.")
@api_url_option()
def cancel_run(workflow_run_id: str, reason: Optional[str], api_url: str) -> None:
    """Cancel a run (POST /api/v1/workflows/runs/{id}/cancel)."""
    base = normalize_base_url(api_url)
    payload: Dict[str, Any] = {}
    if reason:
        payload["reason"] = reason
    r = api_request(
        "POST",
        f"{base}/api/v1/workflows/runs/{workflow_run_id}/cancel",
        headers=get_api_headers(),
        json=payload,
        timeout=API_TIMEOUT,
    )
    click.echo(json.dumps(r.json(), indent=2))


@runs_group.command("resume")
@click.argument("workflow_run_id")
@click.option(
    "--kind",
    type=click.Choice(RESUME_KINDS, case_sensitive=False),
    default=None,
    help="Resume kind (required unless --payload supplies it).",
)
@click.option("--resume-epoch", type=int, default=None, help="Optional expected resume_epoch.")
@click.option(
    "--observations",
    default=None,
    help="JSON array of handback observations [{tool_call_id, content}, ...].",
)
@click.option("--answers", default=None, help="JSON object of elicitation answers.")
@click.option("--decision", default=None, help="approve | reject for kind=confirmation.")
@click.option("--edited-parameters", default=None, help="JSON object of edited tool params.")
@click.option("--auth-status", default=None, help="completed | failed for kind=oauth.")
@click.option(
    "--payload",
    default=None,
    help="Full JSON resume body (or '-' for stdin). Overrides other resume fields when set.",
)
@api_url_option()
def resume_run(
    workflow_run_id: str,
    kind: Optional[str],
    resume_epoch: Optional[int],
    observations: Optional[str],
    answers: Optional[str],
    decision: Optional[str],
    edited_parameters: Optional[str],
    auth_status: Optional[str],
    payload: Optional[str],
    api_url: str,
) -> None:
    """Resume a paused run (POST /api/v1/workflows/runs/{id}/resume)."""
    base = normalize_base_url(api_url)

    if payload is not None:
        raw = sys.stdin.read() if payload == "-" else payload
        body = _parse_json_option(raw, label="payload")
        if not isinstance(body, dict):
            raise click.ClickException("--payload must be a JSON object")
    else:
        if not kind:
            raise click.ClickException("--kind is required unless --payload is provided")
        body = {"kind": kind}
        if resume_epoch is not None:
            body["resume_epoch"] = resume_epoch
        obs = _parse_json_option(observations, label="observations")
        if obs is not None:
            if not isinstance(obs, list):
                raise click.ClickException("--observations must be a JSON array")
            body["observations"] = obs
        ans = _parse_json_option(answers, label="answers")
        if ans is not None:
            if not isinstance(ans, dict):
                raise click.ClickException("--answers must be a JSON object")
            body["answers"] = ans
        if decision is not None:
            body["decision"] = decision
        edited = _parse_json_option(edited_parameters, label="edited-parameters")
        if edited is not None:
            if not isinstance(edited, dict):
                raise click.ClickException("--edited-parameters must be a JSON object")
            body["edited_parameters"] = edited
        if auth_status is not None:
            body["auth_status"] = auth_status

    r = api_request(
        "POST",
        f"{base}/api/v1/workflows/runs/{workflow_run_id}/resume",
        headers=get_api_headers(),
        json=body,
        timeout=RESUME_TIMEOUT,
    )
    click.echo(json.dumps(r.json(), indent=2))


__all__ = ["workflows_group"]
