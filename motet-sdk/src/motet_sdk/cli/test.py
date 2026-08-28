"""
Motet - Command Testing CLI

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Apache License, Version 2.0.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-02-21

Description:
    CLI command for testing commands locally before deployment.

Dependencies:
    - click: CLI framework
    - motet.cli.testing: Testing utilities

Usage:
    motet-cli command test my_command --file=./commands/my_command.py --data='{"input_value": "test"}'

Notes:
    - Provides local testing without deployment
    - Uses mock MotetContext
    - Validates command structure and execution
    - Use --file to point at a command module (e.g. in a bundle's commands/ dir).
"""

import json
from pathlib import Path
from typing import Optional
import click

from .testing import create_mock_motet, run_command_test, validate_command_structure


def _find_command_file(command_name: str) -> Optional[str]:
    """
    Find command file by searching bundle layout and legacy plugin dirs.
    Returns path to first file that appears to define the command.
    """
    # Bundle layout: commands/{command_name}.py or commands/{name}.py
    for base in [Path("."), Path("tests/bundles"), Path("motet-sdk/examples/bundles")]:
        if not base.exists():
            continue
        if base.name == "bundles":
            for bundle_dir in base.iterdir():
                if bundle_dir.is_dir():
                    cmd_file = bundle_dir / "commands" / f"{command_name}.py"
                    if cmd_file.exists():
                        return str(cmd_file)
                    cmd_file = bundle_dir / "commands" / f"{command_name.replace('-', '_')}.py"
                    if cmd_file.exists():
                        return str(cmd_file)
        else:
            cmd_file = base / "commands" / f"{command_name}.py"
            if cmd_file.exists():
                return str(cmd_file)
            cmd_file = base / "commands" / f"{command_name.replace('-', '_')}.py"
            if cmd_file.exists():
                return str(cmd_file)
    # Legacy plugin layout: motet_plugins/command_plugins/*/command.py
    for search_path in [Path("motet_plugins/command_plugins"), Path("plugins/command_plugins")]:
        if not search_path.exists():
            continue
        for plugin_dir in search_path.iterdir():
            if not plugin_dir.is_dir():
                continue
            command_file = plugin_dir / "command.py"
            if command_file.exists():
                try:
                    content = command_file.read_text()
                    if command_name in content or command_name.replace("_", "-") in content:
                        return str(command_file)
                except Exception:
                    pass
    return None


@click.command("test")
@click.argument("command_name")
@click.option(
    "--file",
    type=click.Path(exists=True, file_okay=True, dir_okay=False),
    help="Path to command file (default: auto-detect from cwd/tests/bundles/motet-sdk/examples/bundles)",
)
@click.option(
    "--data",
    help="JSON data for command (default: use example from data class)",
)
@click.option(
    "--mock-tools-result",
    help="JSON result for mock tools.execute()",
)
@click.option(
    "--mock-agent-result",
    help="Text result for mock agent.run()",
)
def test_command_cli(
    command_name: str,
    file: Optional[str],
    data: Optional[str],
    mock_tools_result: Optional[str],
    mock_agent_result: Optional[str],
) -> None:
    """Test command locally without deployment."""
    try:
        # Find command file
        if not file:
            file = _find_command_file(command_name)
            if not file:
                click.echo(
                    f"❌ Command file not found for '{command_name}'. "
                    "Use --file path/to/command.py (e.g. ./commands/my_command.py)",
                    err=True,
                )
                raise click.Abort()
        
        # Load and execute command module
        import importlib.util
        spec = importlib.util.spec_from_file_location("command_module", file)
        if spec is None or spec.loader is None:
            click.echo(f"❌ Could not load module from {file}", err=True)
            raise click.Abort()
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        
        # Find command function
        command_func = None
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if callable(attr) and hasattr(attr, '__name__') and command_name in attr.__name__:
                if hasattr(attr, '__wrapped__') or 'distributed_command' in str(type(attr)):
                    command_func = attr
                    break
        
        if not command_func:
            click.echo(f"❌ Command function not found in {file}", err=True)
            raise click.Abort()
        
        # Validate structure
        validation = validate_command_structure(command_func)
        if not validation["valid"]:
            click.echo(f"❌ Command structure validation failed:", err=True)
            for error in validation["errors"]:
                click.echo(f"   • {error}", err=True)
            raise click.Abort()
        
        # Get data class
        data_class_name = validation["metadata"].get("data_class", "CommandData")
        data_class = getattr(module, data_class_name, None)
        
        if not data_class:
            click.echo(f"❌ Data class '{data_class_name}' not found", err=True)
            raise click.Abort()
        
        # Parse input data
        if data:
            try:
                data_dict = json.loads(data)
            except json.JSONDecodeError as e:
                click.echo(f"❌ Invalid JSON data: {e}", err=True)
                raise click.Abort()
        else:
            # Use example from data class fields
            data_dict = {}
            for field_name, field_info in data_class.__fields__.items():
                if hasattr(field_info, 'field_info') and hasattr(field_info.field_info, 'default'):
                    if field_info.field_info.default is not None:
                        data_dict[field_name] = field_info.field_info.default
                    elif hasattr(field_info.field_info, 'default_factory'):
                        data_dict[field_name] = field_info.field_info.default_factory()
                # Use example if available
                if 'example' in str(field_info):
                    data_dict[field_name] = "example"
        
        command_data = data_class(**data_dict)
        
        # Create mock motet
        mock_tools = json.loads(mock_tools_result) if mock_tools_result else None
        mock_agent = mock_agent_result
        
        mock_motet = create_mock_motet(
            tools_result=mock_tools,
            agent_result=mock_agent
        )
        
        # Test command
        click.echo(f"🧪 Testing command '{command_name}'...\n")
        click.echo(f"📝 Input data: {command_data.dict()}\n")
        
        result = run_command_test(command_func, command_data, mock_motet)
        
        click.echo(f"✅ Command executed successfully\n")
        click.echo(f"📋 Result:")
        click.echo(json.dumps(result, indent=2))
        
    except Exception as e:
        click.echo(f"❌ Error testing command: {e}", err=True)
        import traceback
        traceback.print_exc()
        raise click.Abort()

