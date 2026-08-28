"""
Motet - Built-in Workflow Registrations

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-20

Description:
    Defines and registers built-in workflow templates that ship with the
    orchestration runtime.

Dependencies:
    - Workflow/WorkflowStep/WorkflowRegistry passed at runtime by package init

Usage:
    register_builtin_workflows(Workflow, WorkflowStep, WorkflowRegistry)

Notes:
    - Registration errors are intentionally swallowed to preserve startup.
"""

from __future__ import annotations

from typing import Any


def register_builtin_workflows(Workflow: Any, WorkflowStep: Any, WorkflowRegistry: Any) -> None:
    """Register built-in workflows into the provided registry."""
    _navigate_screenshot_workflow = Workflow(
        workflow_id="navigate_screenshot",
        name="Navigate and Screenshot",
        description=(
            "Navigate to a URL and take a screenshot. Omits filename so "
            "@playwright/mcp returns PNG image content in-band (required for "
            "vision / ui_verify). Passing filename writes under the MCP "
            "container only and suppresses the image payload."
        ),
        keywords=["browser", "playwright", "url", "screenshot", "navigate", "website"],
        required_inputs=["url"],
        input_parameters={
            "url": {
                "type": "string",
                "description": "URL to navigate to and take a screenshot of",
                "examples": ["https://example.com", "https://cantina.co"],
            },
        },
        steps={
            "navigate": WorkflowStep(
                step_id="navigate",
                name="Navigate to URL",
                command_type="core.tool_execution",
                command_data={"tool_name": "mcp.playwright.browser_navigate", "parameters": {"url": "{url}"}},
                execution_context={"required_capabilities": ["BROWSER_OPERATIONS"], "timeout_seconds": 30},
                dependencies=[],
            ),
            "screenshot": WorkflowStep(
                step_id="screenshot",
                name="Take Screenshot",
                command_type="core.tool_execution",
                command_data={
                    "tool_name": "mcp.playwright.browser_take_screenshot",
                    # No filename: MS MCP returns content[].type=image for Motet vision.
                    "parameters": {
                        "type": "png",
                        "fullPage": True,
                    },
                },
                execution_context={"required_capabilities": ["BROWSER_OPERATIONS"], "timeout_seconds": 10},
                dependencies=["navigate"],
            ),
        },
    )

    try:
        WorkflowRegistry.register(_navigate_screenshot_workflow)
    except Exception:
        pass  # builtin registration optional; may already exist

    _web_to_workspace_workflow = Workflow(
        workflow_id="web_to_workspace",
        name="Web Data to Google Workspace",
        description="Extract data from web pages and create Google Workspace documents. Only use when a request is to save the data to a Google Document.",
        required_inputs=["source_url"],
        input_parameters={
            "source_url": {
                "type": "string",
                "description": "URL of the web page to extract data from",
                "examples": ["https://www.cnn.com", "https://news.ycombinator.com"],
            },
            "document_title": {
                "type": "string",
                "description": "Title for the Google Doc to create",
                "default_expression": "Web Extract: {source_url}",
            },
        },
        steps={
            "fetch_page": WorkflowStep(
                step_id="fetch_page",
                name="Fetch Page Content",
                command_type="core.tool_execution",
                command_data={"tool_name": "core.http_get_browser", "parameters": {"url": "{source_url}"}},
                execution_context={"timeout_seconds": 45},
                dependencies=[],
            ),
            "create_doc": WorkflowStep(
                step_id="create_doc",
                name="Create Google Document",
                command_type="core.tool_execution",
                command_data={"tool_name": "mcp.google_workspace.create_doc", "parameters": {"title": "{document_title}"}},
                execution_context={"timeout_seconds": 10},
                dependencies=["fetch_page"],
            ),
            "extract_doc_id": WorkflowStep(
                step_id="extract_doc_id",
                name="Extract Document ID",
                command_type="core.transform",
                command_data={
                    "input": "{create_doc.result.content[0].text}",
                    "operations": [
                        {"type": "regex_extract", "pattern": r"ID: ([\w-]+)", "group": 1, "output_key": "document_id"},
                        {
                            "type": "regex_extract",
                            "pattern": r"Link: (https://[^\s]+)",
                            "group": 1,
                            "output_key": "document_url",
                        },
                    ],
                },
                dependencies=["create_doc"],
            ),
            "add_content": WorkflowStep(
                step_id="add_content",
                name="Add Content to Document",
                command_type="core.tool_execution",
                command_data={
                    "tool_name": "mcp.google_workspace.modify_doc_text",
                    "parameters": {
                        "document_id": "{extract_doc_id.document_id}",
                        "start_index": 1,
                        "text": "{fetch_page.result.main_content}",
                    },
                },
                execution_context={"timeout_seconds": 30},
                dependencies=["extract_doc_id"],
            ),
            "share_doc": WorkflowStep(
                step_id="share_doc",
                name="Share Document",
                command_type="core.tool_execution",
                command_data={
                    "tool_name": "mcp.google_workspace.get_drive_file_permissions",
                    "parameters": {"file_id": "{extract_doc_id.document_id}"},
                },
                execution_context={"timeout_seconds": 10},
                dependencies=["add_content"],
            ),
        },
    )

    try:
        WorkflowRegistry.register(_web_to_workspace_workflow)
    except Exception:
        pass  # builtin registration optional; may already exist

    _research_to_sheets_workflow = Workflow(
        workflow_id="research_to_sheets",
        name="Research Data to Google Sheets",
        description="Search the web, extract data, and populate Google Sheets. Only use when a request is to save the resultsto a Google Sheet.",
        required_inputs=["research_topic", "sheet_title"],
        input_parameters={
            "research_topic": {
                "type": "string",
                "description": "Topic to research using web search (e.g., 'latest AI model releases 2024')",
                "examples": ["AI model releases 2024", "quantum computing breakthroughs"],
            },
            "sheet_title": {
                "type": "string",
                "description": "Title for the Google Sheet to create",
                "minLength": 1,
                "maxLength": 100,
                "default_expression": "Research: {research_topic}",
            },
        },
        steps={
            "search_topic": WorkflowStep(
                step_id="search_topic",
                name="Search Web for Topic",
                command_type="core.tool_execution",
                command_data={"tool_name": "core.web_search", "parameters": {"query": "{research_topic}", "max_results": 5}},
                execution_context={"timeout_seconds": 30},
                dependencies=[],
            ),
            "create_sheet": WorkflowStep(
                step_id="create_sheet",
                name="Create Google Sheet",
                command_type="core.tool_execution",
                command_data={
                    "tool_name": "mcp.google_workspace.create_spreadsheet",
                    "parameters": {"title": "{sheet_title}"},
                },
                execution_context={"timeout_seconds": 10},
                dependencies=["search_topic"],
            ),
            "extract_sheet_id": WorkflowStep(
                step_id="extract_sheet_id",
                name="Extract Spreadsheet ID",
                command_type="core.transform",
                command_data={
                    "input": "{create_sheet.result.content[0].text}",
                    "operations": [
                        {"type": "regex_extract", "pattern": r"ID: ([\w-]+)", "group": 1, "output_key": "spreadsheet_id"},
                        {
                            "type": "regex_extract",
                            "pattern": r"URL: (https://[^\s]+)",
                            "group": 1,
                            "output_key": "spreadsheet_url",
                        },
                    ],
                },
                dependencies=["create_sheet"],
            ),
            "format_sheet": WorkflowStep(
                step_id="format_sheet",
                name="Format Sheet Header",
                command_type="core.tool_execution",
                command_data={
                    "tool_name": "mcp.google_workspace.modify_sheet_values",
                    "parameters": {
                        "spreadsheet_id": "{extract_sheet_id.spreadsheet_id}",
                        "range_name": "A1",
                        "values": [["{sheet_title}"]],
                        "value_input_option": "USER_ENTERED",
                    },
                },
                execution_context={"timeout_seconds": 10},
                dependencies=["extract_sheet_id"],
            ),
            "format_search_data": WorkflowStep(
                step_id="format_search_data",
                name="Parse Search Results into Rows",
                command_type="core.transform",
                command_data={
                    "input": "{search_topic.result.main_content}",
                    "operations": [
                        {"type": "split", "pattern": " | ", "output_key": "result_sections"},
                        {"type": "to_rows", "output_key": "rows_array"},
                    ],
                },
                dependencies=["search_topic"],
            ),
            "populate_data": WorkflowStep(
                step_id="populate_data",
                name="Populate Sheet with Data",
                command_type="core.tool_execution",
                command_data={
                    "tool_name": "mcp.google_workspace.modify_sheet_values",
                    "parameters": {
                        "spreadsheet_id": "{extract_sheet_id.spreadsheet_id}",
                        "range_name": "A2",
                        "values": "{format_search_data.rows_array}",
                    },
                },
                execution_context={"timeout_seconds": 10},
                dependencies=["extract_sheet_id", "format_search_data"],
            ),
        },
    )

    try:
        WorkflowRegistry.register(_research_to_sheets_workflow)
    except Exception:
        pass  # builtin registration optional; may already exist

