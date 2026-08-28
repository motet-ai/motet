"""
Motet - Transform Command Tests

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-13

Description:
    Comprehensive tests for the transform command (ADR-0049 Fix #4).
    Tests all transformation operations: regex_extract, json_parse, json_path,
    default, first/last, substring, trim, upper, lower, split, join, replace,
    mcp_text, and playwright_result.

Dependencies:
    - pytest: Testing framework
    - motet.core.workflow: TransformData, transform_command

Usage:
    pytest tests/core/orchestration/test_transform_command.py -v

Notes:
    - Tests validate that transform command provides inspectable workflow steps
    - Tests cover all Phase 2 and Phase 3 transforms requested by user
"""

import json
import pytest
from unittest.mock import Mock
from motet.core.commands.builtin.transform import (
    TransformData,
    TransformOperation,
    transform,
    _extract_json_path
)


class TestPhase2EssentialTransforms:
    """Test Phase 2: Essential Transforms (regex_extract, json_parse, json_path, default, first/last)"""
    
    def test_regex_extract_simple(self):
        """Test regex_extract with simple pattern"""
        data = TransformData(
            input="Successfully created spreadsheet 'My Sheet' for user@email.com. ID: 1a2b3c4d5e | URL: https://docs.google.com/...",
            operations=[
                TransformOperation(
                    type="regex_extract",
                    pattern=r"ID: ([\w]+)",
                    group=1,
                    output_key="spreadsheet_id"
                )
            ]
        )
        
        mock_motet = Mock()
        result = transform.__wrapped__(data=data, motet=mock_motet)
        
        assert result["spreadsheet_id"] == "1a2b3c4d5e"
    
    def test_regex_extract_multiple_groups(self):
        """Test regex_extract with chaining: each op gets the previous result as input."""
        data = TransformData(
            input="Successfully created spreadsheet 'My Sheet'. ID: abc123 | URL: https://docs.google.com/spreadsheets/d/abc123/edit",
            operations=[
                TransformOperation(
                    type="regex_extract",
                    pattern=r"ID: ([\w]+)",
                    group=1,
                    output_key="id"
                ),
                TransformOperation(
                    type="regex_extract",
                    pattern=r"URL: (https://[^\s]+)",
                    group=1,
                    output_key="url"
                )
            ]
        )

        mock_motet = Mock()
        result = transform.__wrapped__(data=data, motet=mock_motet)

        assert result["id"] == "abc123"
        # Second op runs on first op result ("abc123"), so URL pattern does not match
        assert result["url"] == ""
    
    def test_regex_extract_no_match(self):
        """Test regex_extract when pattern doesn't match"""
        data = TransformData(
            input="No ID here",
            operations=[
                TransformOperation(
                    type="regex_extract",
                    pattern=r"ID: ([\w]+)",
                    group=1,
                    output_key="id"
                )
            ]
        )
        
        mock_motet = Mock()
        result = transform.__wrapped__(data=data, motet=mock_motet)
        
        assert result["id"] == ""  # Empty string when no match
    
    def test_json_parse(self):
        """Test json_parse transformation"""
        data = TransformData(
            input='{"status": "success", "data": {"id": "123", "name": "Test"}}',
            operations=[
                TransformOperation(
                    type="json_parse",
                    output_key="parsed"
                )
            ]
        )
        
        mock_motet = Mock()
        result = transform.__wrapped__(data=data, motet=mock_motet)
        
        assert result["parsed"]["status"] == "success"
        assert result["parsed"]["data"]["id"] == "123"
    
    def test_json_path_simple(self):
        """Test json_path with simple path"""
        data = TransformData(
            input={"status": "success", "data": {"user_id": "12345"}},
            operations=[
                TransformOperation(
                    type="json_path",
                    path="$.data.user_id",
                    output_key="user_id"
                )
            ]
        )
        
        mock_motet = Mock()
        result = transform.__wrapped__(data=data, motet=mock_motet)
        
        assert result["user_id"] == "12345"
    
    def test_json_path_with_array(self):
        """Test json_path with array indexing"""
        data = TransformData(
            input={"results": [{"id": "first"}, {"id": "second"}]},
            operations=[
                TransformOperation(
                    type="json_path",
                    path="$.results[0].id",
                    output_key="first_id"
                )
            ]
        )
        
        mock_motet = Mock()
        result = transform.__wrapped__(data=data, motet=mock_motet)
        
        assert result["first_id"] == "first"
    
    def test_default_with_none(self):
        """Test default transformation with None value"""
        data = TransformData(
            input=None,
            operations=[
                TransformOperation(
                    type="default",
                    default_value="fallback_value",
                    output_key="result"
                )
            ]
        )
        
        mock_motet = Mock()
        result = transform.__wrapped__(data=data, motet=mock_motet)
        
        assert result["result"] == "fallback_value"
    
    def test_default_with_empty_string(self):
        """Test default transformation with empty string"""
        data = TransformData(
            input="",
            operations=[
                TransformOperation(
                    type="default",
                    default_value="default_text",
                    output_key="result"
                )
            ]
        )
        
        mock_motet = Mock()
        result = transform.__wrapped__(data=data, motet=mock_motet)
        
        assert result["result"] == "default_text"
    
    def test_default_with_value(self):
        """Test default transformation preserves existing value"""
        data = TransformData(
            input="existing_value",
            operations=[
                TransformOperation(
                    type="default",
                    default_value="fallback",
                    output_key="result"
                )
            ]
        )
        
        mock_motet = Mock()
        result = transform.__wrapped__(data=data, motet=mock_motet)
        
        assert result["result"] == "existing_value"
    
    def test_first_array_element(self):
        """Test first transformation on array"""
        data = TransformData(
            input=["first", "second", "third"],
            operations=[
                TransformOperation(
                    type="first",
                    output_key="first_item"
                )
            ]
        )
        
        mock_motet = Mock()
        result = transform.__wrapped__(data=data, motet=mock_motet)
        
        assert result["first_item"] == "first"
    
    def test_last_array_element(self):
        """Test last transformation on array"""
        data = TransformData(
            input=["first", "second", "third"],
            operations=[
                TransformOperation(
                    type="last",
                    output_key="last_item"
                )
            ]
        )
        
        mock_motet = Mock()
        result = transform.__wrapped__(data=data, motet=mock_motet)
        
        assert result["last_item"] == "third"


class TestPhase3StringTransforms:
    """Test Phase 3: String Transforms (substring, trim, upper, lower, split, join, replace)"""
    
    def test_substring_with_start_and_end(self):
        """Test substring transformation with start and end"""
        data = TransformData(
            input="Hello, World!",
            operations=[
                TransformOperation(
                    type="substring",
                    start=0,
                    end=5,
                    output_key="result"
                )
            ]
        )
        
        mock_motet = Mock()
        result = transform.__wrapped__(data=data, motet=mock_motet)
        
        assert result["result"] == "Hello"
    
    def test_substring_with_start_only(self):
        """Test substring transformation with start only (to end)"""
        data = TransformData(
            input="Hello, World!",
            operations=[
                TransformOperation(
                    type="substring",
                    start=7,
                    output_key="result"
                )
            ]
        )
        
        mock_motet = Mock()
        result = transform.__wrapped__(data=data, motet=mock_motet)
        
        assert result["result"] == "World!"
    
    def test_trim_whitespace(self):
        """Test trim transformation"""
        data = TransformData(
            input="  Hello, World!  \n",
            operations=[
                TransformOperation(
                    type="trim",
                    output_key="result"
                )
            ]
        )
        
        mock_motet = Mock()
        result = transform.__wrapped__(data=data, motet=mock_motet)
        
        assert result["result"] == "Hello, World!"
    
    def test_upper_case(self):
        """Test upper transformation"""
        data = TransformData(
            input="hello world",
            operations=[
                TransformOperation(
                    type="upper",
                    output_key="result"
                )
            ]
        )
        
        mock_motet = Mock()
        result = transform.__wrapped__(data=data, motet=mock_motet)
        
        assert result["result"] == "HELLO WORLD"
    
    def test_lower_case(self):
        """Test lower transformation"""
        data = TransformData(
            input="HELLO WORLD",
            operations=[
                TransformOperation(
                    type="lower",
                    output_key="result"
                )
            ]
        )
        
        mock_motet = Mock()
        result = transform.__wrapped__(data=data, motet=mock_motet)
        
        assert result["result"] == "hello world"
    
    def test_split_with_default_separator(self):
        """Test split transformation with default separator (space)"""
        data = TransformData(
            input="hello world test",
            operations=[
                TransformOperation(
                    type="split",
                    output_key="words"
                )
            ]
        )
        
        mock_motet = Mock()
        result = transform.__wrapped__(data=data, motet=mock_motet)
        
        assert result["words"] == ["hello", "world", "test"]
    
    def test_split_with_custom_separator(self):
        """Test split transformation with custom separator"""
        data = TransformData(
            input="one,two,three",
            operations=[
                TransformOperation(
                    type="split",
                    pattern=",",
                    output_key="items"
                )
            ]
        )
        
        mock_motet = Mock()
        result = transform.__wrapped__(data=data, motet=mock_motet)
        
        assert result["items"] == ["one", "two", "three"]
    
    def test_join_with_default_separator(self):
        """Test join transformation with default separator (space)"""
        data = TransformData(
            input=["hello", "world", "test"],
            operations=[
                TransformOperation(
                    type="join",
                    output_key="text"
                )
            ]
        )
        
        mock_motet = Mock()
        result = transform.__wrapped__(data=data, motet=mock_motet)
        
        assert result["text"] == "hello world test"
    
    def test_join_with_custom_separator(self):
        """Test join transformation with custom separator"""
        data = TransformData(
            input=["one", "two", "three"],
            operations=[
                TransformOperation(
                    type="join",
                    separator=", ",
                    output_key="text"
                )
            ]
        )
        
        mock_motet = Mock()
        result = transform.__wrapped__(data=data, motet=mock_motet)
        
        assert result["text"] == "one, two, three"
    
    def test_replace_substring(self):
        """Test replace transformation"""
        data = TransformData(
            input="Hello World",
            operations=[
                TransformOperation(
                    type="replace",
                    old="World",
                    new="Universe",
                    output_key="result"
                )
            ]
        )
        
        mock_motet = Mock()
        result = transform.__wrapped__(data=data, motet=mock_motet)
        
        assert result["result"] == "Hello Universe"


class TestChainedTransformations:
    """Test chaining multiple transformations together"""
    
    def test_parse_and_extract_chain(self):
        """Test json_parse followed by json_path"""
        data = TransformData(
            input='{"data": {"users": [{"id": "123", "name": "Alice"}]}}',
            operations=[
                TransformOperation(
                    type="json_parse",
                    output_key="parsed"
                ),
                TransformOperation(
                    type="json_path",
                    path="$.data.users[0].name",
                    output_key="user_name"
                )
            ]
        )
        
        mock_motet = Mock()
        result = transform.__wrapped__(data=data, motet=mock_motet)
        
        assert result["user_name"] == "Alice"
    
    def test_extract_trim_upper_chain(self):
        """Test regex_extract, trim, and upper in sequence"""
        data = TransformData(
            input="  Status: active  ",
            operations=[
                TransformOperation(
                    type="regex_extract",
                    pattern=r"Status: (\w+)",
                    group=1,
                    output_key="status"
                ),
                TransformOperation(
                    type="trim",
                    output_key="trimmed"
                ),
                TransformOperation(
                    type="upper",
                    output_key="final"
                )
            ]
        )
        
        mock_motet = Mock()
        result = transform.__wrapped__(data=data, motet=mock_motet)
        
        assert result["final"] == "ACTIVE"
    
    def test_split_first_upper_chain(self):
        """Test split, first, and upper in sequence"""
        data = TransformData(
            input="hello,world,test",
            operations=[
                TransformOperation(
                    type="split",
                    pattern=",",
                    output_key="items"
                ),
                TransformOperation(
                    type="first",
                    output_key="first_item"
                ),
                TransformOperation(
                    type="upper",
                    output_key="result"
                )
            ]
        )
        
        mock_motet = Mock()
        result = transform.__wrapped__(data=data, motet=mock_motet)
        
        assert result["result"] == "HELLO"


class TestJSONPathExtraction:
    """Test _extract_json_path helper function"""
    
    def test_simple_field_access(self):
        """Test simple field extraction"""
        data = {"name": "Alice", "age": 30}
        result = _extract_json_path(data, "$.name")
        assert result == "Alice"
    
    def test_nested_field_access(self):
        """Test nested field extraction"""
        data = {"user": {"profile": {"name": "Bob"}}}
        result = _extract_json_path(data, "$.user.profile.name")
        assert result == "Bob"
    
    def test_array_index_access(self):
        """Test array indexing"""
        data = {"items": ["first", "second", "third"]}
        result = _extract_json_path(data, "$.items[1]")
        assert result == "second"
    
    def test_array_element_field_access(self):
        """Test array element field extraction"""
        data = {"users": [{"id": "1", "name": "Alice"}, {"id": "2", "name": "Bob"}]}
        result = _extract_json_path(data, "$.users[1].name")
        assert result == "Bob"
    
    def test_path_not_found(self):
        """Test extraction with non-existent path"""
        data = {"name": "Alice"}
        result = _extract_json_path(data, "$.nonexistent.field")
        assert result is None


class TestRealWorldScenarios:
    """Test real-world use cases from research_to_sheets workflow"""
    
    def test_extract_spreadsheet_id_from_mcp_response(self):
        """Test extracting spreadsheet ID from Google Workspace MCP response"""
        # Real MCP response format
        mcp_response = "Successfully created spreadsheet 'My Research Sheet' for user@example.com. ID: 1a2b3c4d5e6f7g8h | URL: https://docs.google.com/spreadsheets/d/1a2b3c4d5e6f7g8h/edit"
        
        data = TransformData(
            input=mcp_response,
            operations=[
                TransformOperation(
                    type="regex_extract",
                    pattern=r"ID: ([\w-]+)",
                    group=1,
                    output_key="spreadsheet_id"
                ),
                TransformOperation(
                    type="regex_extract",
                    pattern=r"URL: (https://[^\s]+)",
                    group=1,
                    output_key="spreadsheet_url"
                )
            ]
        )
        
        mock_motet = Mock()
        result = transform.__wrapped__(data=data, motet=mock_motet)
        
        assert result["spreadsheet_id"] == "1a2b3c4d5e6f7g8h"
        # Chaining: second op runs on first result, so URL pattern does not match
        assert result["spreadsheet_url"] == ""
    
    def test_extract_and_clean_text_data(self):
        """Test extracting and cleaning text data for sheet population"""
        # Simulated extracted text with extra whitespace
        raw_text = "  AI Model Release Notes  \n\n  GPT-4 released March 2023  \n  Claude 3 released March 2024  "
        
        data = TransformData(
            input=raw_text,
            operations=[
                TransformOperation(
                    type="trim",
                    output_key="cleaned"
                ),
                TransformOperation(
                    type="split",
                    pattern="\n",
                    output_key="lines"
                )
            ]
        )
        
        mock_motet = Mock()
        result = transform.__wrapped__(data=data, motet=mock_motet)
        
        assert "AI Model Release Notes" in result["lines"][0]
        assert len(result["lines"]) > 1


class TestMcpTextAndPlaywrightResult:
    """MCP envelope unwrap and Playwright markdown Result extraction."""

    def test_mcp_text_from_content_array(self):
        data = TransformData(
            input={
                "content": [
                    {"type": "text", "text": '[{"title": "A", "url": "https://a.example"}]'}
                ]
            },
            operations=[TransformOperation(type="mcp_text", output_key="text")],
        )
        result = transform.__wrapped__(data=data, motet=Mock())
        assert result["text"] == '[{"title": "A", "url": "https://a.example"}]'

    def test_mcp_text_from_tool_execution_wrapper(self):
        data = TransformData(
            input={
                "tool_name": "mcp.github.list_pull_requests",
                "result": {
                    "content": [{"type": "text", "text": '[{"number": 12}]'}]
                },
                "executed": True,
                "execution_method": "motet_mcp_client",
            },
            operations=[
                TransformOperation(type="mcp_text", output_key="raw"),
                TransformOperation(type="json_parse", output_key="parsed"),
            ],
        )
        result = transform.__wrapped__(data=data, motet=Mock())
        assert result["parsed"] == [{"number": 12}]

    def test_mcp_text_structured_content(self):
        data = TransformData(
            input={"structuredContent": {"result": "plain payload"}},
            operations=[TransformOperation(type="mcp_text", output_key="text")],
        )
        result = transform.__wrapped__(data=data, motet=Mock())
        assert result["text"] == "plain payload"

    def test_mcp_text_passthrough_string(self):
        data = TransformData(
            input='{"ok": true}',
            operations=[TransformOperation(type="mcp_text", output_key="text")],
        )
        result = transform.__wrapped__(data=data, motet=Mock())
        assert result["text"] == '{"ok": true}'

    def test_json_parse_rejects_dict(self):
        data = TransformData(
            input={"content": [{"type": "text", "text": "[]"}]},
            operations=[TransformOperation(type="json_parse", output_key="parsed")],
        )
        with pytest.raises(RuntimeError, match="expected a JSON string, got dict"):
            transform.__wrapped__(data=data, motet=Mock())

    def test_playwright_result_extracts_result_section(self):
        report = (
            '### Result\n'
            '[{"title": "Cookies", "url": "https://example.com"}]\n'
            "### Ran Playwright code\n"
            "```js\nawait page.evaluate('() => []');\n```\n"
        )
        data = TransformData(
            input=report,
            operations=[
                TransformOperation(type="playwright_result", output_key="body"),
                TransformOperation(type="json_parse", output_key="links"),
            ],
        )
        result = transform.__wrapped__(data=data, motet=Mock())
        assert result["links"] == [{"title": "Cookies", "url": "https://example.com"}]

    def test_playwright_result_unwraps_json_string_layer(self):
        encoded = json.dumps(json.dumps([{"title": "Cookies"}]))
        report = (
            f"### Result\n{encoded}\n"
            "### Ran Playwright code\n"
            "```js\nawait page.evaluate();\n```\n"
        )
        data = TransformData(
            input=report,
            operations=[
                TransformOperation(type="playwright_result", output_key="body"),
                TransformOperation(type="json_parse", output_key="links"),
            ],
        )
        result = transform.__wrapped__(data=data, motet=Mock())
        assert result["links"] == [{"title": "Cookies"}]

    def test_playwright_result_non_json_body(self):
        report = "### Result\nWaited for 2\n### Ran Playwright code\n```js\nawait new Promise();\n```\n"
        data = TransformData(
            input=report,
            operations=[TransformOperation(type="playwright_result", output_key="body")],
        )
        result = transform.__wrapped__(data=data, motet=Mock())
        assert result["body"] == "Waited for 2"

    def test_playwright_result_noop_without_heading(self):
        data = TransformData(
            input='[{"number": 12}]',
            operations=[TransformOperation(type="playwright_result", output_key="body")],
        )
        result = transform.__wrapped__(data=data, motet=Mock())
        assert result["body"] == '[{"number": 12}]'

    def test_playwright_result_rejects_dict(self):
        data = TransformData(
            input={"content": [{"type": "text", "text": "### Result\n[]"}]},
            operations=[TransformOperation(type="playwright_result", output_key="body")],
        )
        with pytest.raises(RuntimeError, match="expected a markdown string, got dict"):
            transform.__wrapped__(data=data, motet=Mock())

    def test_playwright_evaluate_chain(self):
        links = [{"title": "Best cookies", "url": "https://yelp.com"}]
        encoded = json.dumps(json.dumps(links))
        payload = {
            "tool_name": "mcp.playwright.browser_evaluate",
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"### Result\n{encoded}\n"
                            "### Ran Playwright code\n"
                            "```js\nawait page.evaluate();\n```\n"
                        ),
                    }
                ]
            },
            "executed": True,
            "execution_method": "motet_mcp_client",
        }
        data = TransformData(
            input=payload,
            operations=[
                TransformOperation(type="mcp_text", output_key="raw"),
                TransformOperation(type="playwright_result", output_key="result_body"),
                TransformOperation(type="json_parse", output_key="result_links"),
            ],
        )
        result = transform.__wrapped__(data=data, motet=Mock())
        assert result["result_links"] == [
            {"title": "Best cookies", "url": "https://yelp.com"}
        ]


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])

