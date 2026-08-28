"""Unit tests for discovery-mode bearer placeholder for Google Workspace MCP stdio."""

from motet.core.tools.mcp_motet.proxy.mcp_instance_manager import (
    AuthType,
    MCPInstanceConfig,
    ServiceAuthConfig,
    _apply_stdio_discovery_bearer_placeholder,
)


def test_placeholder_injected_when_discovery_and_templated_env() -> None:
    cfg = MCPInstanceConfig(
        service_id="google_workspace",
        transport="stdio",
        command="python",
        args=["/app/main.py", "--bearer-token-mode", "--tools", "gmail"],
        env={"GOOGLE_BEARER_TOKEN": "${GOOGLE_BEARER_TOKEN}"},
        auth=ServiceAuthConfig(
            type=AuthType.OAUTH2,
            env_var="GOOGLE_BEARER_TOKEN",
            token_field="access_token",
        ),
    )
    env_vars = dict(cfg.env)
    _apply_stdio_discovery_bearer_placeholder(
        discovery_mode=True,
        service_id="google_workspace",
        service_config=cfg,
        env_vars=env_vars,
    )
    assert env_vars["GOOGLE_BEARER_TOKEN"] == "__MOTET_MCP_DISCOVERY_PLACEHOLDER__"


def test_no_placeholder_when_real_token_present() -> None:
    cfg = MCPInstanceConfig(
        service_id="google_workspace",
        transport="stdio",
        command="python",
        args=["/app/main.py", "--bearer-token-mode"],
        env={"GOOGLE_BEARER_TOKEN": "ya29.real"},
        auth=ServiceAuthConfig(type=AuthType.OAUTH2, env_var="GOOGLE_BEARER_TOKEN"),
    )
    env_vars = dict(cfg.env)
    _apply_stdio_discovery_bearer_placeholder(
        discovery_mode=True,
        service_id="google_workspace",
        service_config=cfg,
        env_vars=env_vars,
    )
    assert env_vars["GOOGLE_BEARER_TOKEN"] == "ya29.real"


def test_no_placeholder_without_bearer_token_mode_flag() -> None:
    cfg = MCPInstanceConfig(
        service_id="google_workspace",
        transport="stdio",
        command="python",
        args=["/app/main.py"],
        env={},
        auth=ServiceAuthConfig(type=AuthType.OAUTH2, env_var="GOOGLE_BEARER_TOKEN"),
    )
    env_vars = dict(cfg.env)
    _apply_stdio_discovery_bearer_placeholder(
        discovery_mode=True,
        service_id="google_workspace",
        service_config=cfg,
        env_vars=env_vars,
    )
    assert "GOOGLE_BEARER_TOKEN" not in env_vars
