"""
Motet - Context Manager

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Context Manager for the Motet distributed framework.

Dependencies:
    - typing: Type hints and annotations
    - Base interfaces and implementations

Usage:
    from motet.core.tools.context_manager import ContextManager

Notes:
    - Provides core functionality
    - Integrates with distributed architecture
"""


from __future__ import annotations

import json
from pydantic import BaseModel, Field
from enum import Enum
from typing import Any, Dict, List, Optional, Callable, Union
from abc import ABC, abstractmethod

from ..registry import parse_qualified_name


class ContextPriority(Enum):
    """Priority levels for context content."""
    CRITICAL = "critical"      # Must include (error info, key data)
    HIGH = "high"             # Important (main content, results)
    MEDIUM = "medium"         # Useful (metadata, headers)
    LOW = "low"              # Optional (debug info, extra details)


class ContextStrategy(Enum):
    """Strategies for handling context overflow."""
    TRUNCATE = "truncate"           # Simple truncation
    SUMMARIZE = "summarize"         # AI-based summarization
    PRIORITIZE = "prioritize"       # Keep high-priority content
    CHUNK = "chunk"                 # Split into multiple contexts
    COMPRESS = "compress"           # Compress/encode content


class ContextRequirement(BaseModel):
    """Defines context requirements for a tool."""
    max_tokens: int = 4000                    # Maximum context tokens
    min_tokens: int = 100                     # Minimum useful context
    preferred_tokens: int = 2000              # Preferred context size
    overflow_strategy: ContextStrategy = ContextStrategy.PRIORITIZE
    allow_chunking: bool = False              # Can handle chunked responses
    content_types: List[str] = Field(default_factory=lambda: ["text"])  # Supported content types
    summarization_prompt: Optional[str] = None  # Custom summarization prompt
    priority_fields: List[str] = Field(default_factory=list)  # High-priority fields in responses


class ContextItem(BaseModel):
    """A piece of content with metadata for context management."""
    content: str
    priority: ContextPriority = ContextPriority.MEDIUM
    content_type: str = "text"
    source: str = "unknown"
    tokens: Optional[int] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ContextProcessor(ABC):
    """Abstract base for context processing strategies."""
    
    @abstractmethod
    def process(
        self, 
        items: List[ContextItem], 
        requirement: ContextRequirement,
        available_tokens: int
    ) -> List[ContextItem]:
        """Process context items according to strategy (synchronous - ADR-0033)."""
        pass


class TruncateProcessor(ContextProcessor):
    """Simple truncation processor."""
    
    def process(
        self, 
        items: List[ContextItem], 
        requirement: ContextRequirement,
        available_tokens: int
    ) -> List[ContextItem]:
        """Truncate content to fit available tokens (synchronous - ADR-0033)."""
        processed = []
        used_tokens = 0
        
        # Sort by priority (critical first)
        priority_order = [ContextPriority.CRITICAL, ContextPriority.HIGH, 
                         ContextPriority.MEDIUM, ContextPriority.LOW]
        sorted_items = sorted(items, key=lambda x: priority_order.index(x.priority))
        
        for item in sorted_items:
            item_tokens = item.tokens or self._estimate_tokens(item.content)
            
            if used_tokens + item_tokens <= available_tokens:
                processed.append(item)
                used_tokens += item_tokens
            else:
                # Try to fit a truncated version
                remaining_tokens = available_tokens - used_tokens
                if remaining_tokens > 50:  # Minimum useful size
                    truncated_content = self._truncate_to_tokens(item.content, remaining_tokens)
                    truncated_item = ContextItem(
                        content=truncated_content,
                        priority=item.priority,
                        content_type=item.content_type,
                        source=f"{item.source}(truncated)",
                        tokens=remaining_tokens,
                        metadata={**item.metadata, "truncated": True}
                    )
                    processed.append(truncated_item)
                break
        
        return processed
    
    def _estimate_tokens(self, content: str) -> int:
        """Rough token estimation (4 chars per token)."""
        return max(1, len(content) // 4)
    
    def _truncate_to_tokens(self, content: str, max_tokens: int) -> str:
        """Truncate content to approximate token count."""
        max_chars = max_tokens * 4
        if len(content) <= max_chars:
            return content
        
        # Try to truncate at word boundaries
        truncated = content[:max_chars]
        last_space = truncated.rfind(' ')
        if last_space > max_chars * 0.8:  # If we can save most content
            truncated = truncated[:last_space]
        
        return truncated + "..."


class PrioritizeProcessor(ContextProcessor):
    """Priority-based context processor."""
    
    def process(
        self, 
        items: List[ContextItem], 
        requirement: ContextRequirement,
        available_tokens: int
    ) -> List[ContextItem]:
        """Keep highest priority content that fits (synchronous - ADR-0033)."""
        # Group by priority
        priority_groups = {p: [] for p in ContextPriority}
        for item in items:
            priority_groups[item.priority].append(item)
        
        processed = []
        used_tokens = 0
        
        # Process in priority order
        for priority in [ContextPriority.CRITICAL, ContextPriority.HIGH, 
                        ContextPriority.MEDIUM, ContextPriority.LOW]:
            for item in priority_groups[priority]:
                item_tokens = item.tokens or self._estimate_tokens(item.content)
                
                if used_tokens + item_tokens <= available_tokens:
                    processed.append(item)
                    used_tokens += item_tokens
                elif priority in [ContextPriority.CRITICAL, ContextPriority.HIGH]:
                    # Force-fit critical/high priority content
                    remaining = available_tokens - used_tokens
                    if remaining > 100:
                        truncated = self._smart_truncate(item, remaining, requirement)
                        processed.append(truncated)
                        used_tokens = available_tokens
                        break
        
        return processed
    
    def _estimate_tokens(self, content: str) -> int:
        """Rough token estimation."""
        return max(1, len(content) // 4)
    
    def _smart_truncate(
        self, 
        item: ContextItem, 
        max_tokens: int, 
        requirement: ContextRequirement
    ) -> ContextItem:
        """Smart truncation preserving important parts."""
        content = item.content
        max_chars = max_tokens * 4
        
        if len(content) <= max_chars:
            return item
        
        # Try to preserve priority fields if specified
        if requirement.priority_fields and item.content_type == "json":
            try:
                data = json.loads(content)
                preserved = {}
                for field in requirement.priority_fields:
                    if field in data:
                        preserved[field] = data[field]
                
                preserved_content = json.dumps(preserved, indent=2)
                if len(preserved_content) <= max_chars:
                    return ContextItem(
                        content=preserved_content,
                        priority=item.priority,
                        content_type=item.content_type,
                        source=f"{item.source}(prioritized)",
                        tokens=max_tokens,
                        metadata={**item.metadata, "prioritized": True, "preserved_fields": requirement.priority_fields}
                    )
            except (json.JSONDecodeError, TypeError):
                pass
        
        # Fallback to simple truncation
        truncated = content[:max_chars]
        last_space = truncated.rfind(' ')
        if last_space > max_chars * 0.8:
            truncated = truncated[:last_space]
        
        return ContextItem(
            content=truncated + "...",
            priority=item.priority,
            content_type=item.content_type,
            source=f"{item.source}(truncated)",
            tokens=max_tokens,
            metadata={**item.metadata, "truncated": True}
        )


class ContextManager:
    """Advanced context management system for tools."""
    
    def __init__(self, stack=None):
        self.stack = stack
        self.processors = {
            ContextStrategy.TRUNCATE: TruncateProcessor(),
            ContextStrategy.PRIORITIZE: PrioritizeProcessor(),
            # TODO: Add SummarizeProcessor, ChunkProcessor, CompressProcessor
        }
        
        # Default context requirements by tool category
        self.default_requirements = {
            "http": ContextRequirement(
                max_tokens=8000,
                preferred_tokens=4000,
                overflow_strategy=ContextStrategy.PRIORITIZE,
                priority_fields=["text", "status", "error"]
            ),
            "filesystem": ContextRequirement(
                max_tokens=16000,
                preferred_tokens=8000,
                overflow_strategy=ContextStrategy.TRUNCATE
            ),
            "memory": ContextRequirement(
                max_tokens=6000,
                preferred_tokens=3000,
                overflow_strategy=ContextStrategy.PRIORITIZE,
                priority_fields=["content", "tags", "type"]
            ),
            "math": ContextRequirement(
                max_tokens=1000,
                preferred_tokens=500,
                overflow_strategy=ContextStrategy.TRUNCATE
            ),
            "system": ContextRequirement(
                max_tokens=4000,
                preferred_tokens=2000,
                overflow_strategy=ContextStrategy.PRIORITIZE
            ),
            "search": ContextRequirement(
                max_tokens=8000,
                preferred_tokens=4000,
                overflow_strategy=ContextStrategy.PRIORITIZE,
                priority_fields=["main_content", "summary", "data"]
            )
        }
    
    def get_context_requirement(self, tool_name: str, tool_category: str = "general") -> ContextRequirement:
        """Get context requirements for a tool."""
        # TODO: Allow tools to register custom requirements
        return self.default_requirements.get(tool_category, ContextRequirement())
    
    def process_tool_response(
        self, 
        response: Dict[str, Any], 
        tool_name: str, 
        tool_category: str = "general",
        available_context_tokens: int = 4000
    ) -> Dict[str, Any]:
        """Process a tool response according to context requirements (synchronous - ADR-0033)."""
        requirement = self.get_context_requirement(tool_name, tool_category)
        
        # Convert response to context items
        items = self._response_to_context_items(response, tool_name)
        
        # Process according to strategy
        processor = self.processors.get(requirement.overflow_strategy, self.processors[ContextStrategy.TRUNCATE])
        processed_items = processor.process(items, requirement, available_context_tokens)
        
        # Convert back to response format
        return self._context_items_to_response(processed_items, response)
    
    def _response_to_context_items(self, response: Dict[str, Any], tool_name: str) -> List[ContextItem]:
        """Convert tool response to context items with priorities."""
        items = []
        
        # Error information is always critical
        if "error" in response:
            items.append(ContextItem(
                content=str(response["error"]),
                priority=ContextPriority.CRITICAL,
                source=f"{tool_name}.error"
            ))
        
        # Status information is high priority
        if "status" in response:
            items.append(ContextItem(
                content=f"Status: {response['status']}",
                priority=ContextPriority.HIGH,
                source=f"{tool_name}.status"
            ))
        
        # Main content varies by response structure
        # Enhanced HTTP tool fields (highest priority)
        if "main_content" in response:
            content = response["main_content"]
            priority = ContextPriority.HIGH if len(content) < 2000 else ContextPriority.MEDIUM
            items.append(ContextItem(
                content=content,
                priority=priority,
                source=f"{tool_name}.main_content",
                tokens=self._estimate_tokens(content)
            ))
        
        if "title" in response and response["title"]:
            items.append(ContextItem(
                content=f"Title: {response['title']}",
                priority=ContextPriority.HIGH,
                source=f"{tool_name}.title"
            ))
        
        if "description" in response and response["description"]:
            items.append(ContextItem(
                content=f"Description: {response['description']}",
                priority=ContextPriority.HIGH,
                source=f"{tool_name}.description"
            ))
        
        if "data" in response:
            content = json.dumps(response["data"], indent=2) if isinstance(response["data"], dict) else str(response["data"])
            items.append(ContextItem(
                content=content,
                priority=ContextPriority.HIGH,
                content_type="json" if isinstance(response["data"], dict) else "text",
                source=f"{tool_name}.data",
                tokens=self._estimate_tokens(content)
            ))
        
        # Standard fields
        if "text" in response:
            content = response["text"]
            priority = ContextPriority.HIGH if len(content) < 1000 else ContextPriority.MEDIUM
            items.append(ContextItem(
                content=content,
                priority=priority,
                source=f"{tool_name}.text",
                tokens=self._estimate_tokens(content)
            ))
        
        if "result" in response:
            content = json.dumps(response["result"], indent=2) if isinstance(response["result"], dict) else str(response["result"])
            items.append(ContextItem(
                content=content,
                priority=ContextPriority.HIGH,
                content_type="json" if isinstance(response["result"], dict) else "text",
                source=f"{tool_name}.result",
                tokens=self._estimate_tokens(content)
            ))
        
        # Headers and metadata are lower priority
        if "headers" in response:
            items.append(ContextItem(
                content=json.dumps(response["headers"], indent=2),
                priority=ContextPriority.LOW,
                content_type="json",
                source=f"{tool_name}.headers"
            ))
        
        # Add any other fields as medium priority
        # For complex structures (dicts, lists), store as JSON to preserve structure
        excluded_fields = ["error", "status", "text", "result", "headers", "main_content", "title", "description", "data"]
        for key, value in response.items():
            if key not in excluded_fields:
                # Store complex structures as JSON to preserve them
                if isinstance(value, (dict, list)):
                    content = json.dumps(value, indent=2)
                    content_type = "json"
                else:
                    content = f"{key}: {value}"
                    content_type = "text"
                items.append(ContextItem(
                    content=content,
                    priority=ContextPriority.MEDIUM,
                    content_type=content_type,
                    source=f"{tool_name}.{key}"
                ))
        
        return items
    
    def _context_items_to_response(self, items: List[ContextItem], original: Dict[str, Any]) -> Dict[str, Any]:
        """Convert processed context items back to response format."""
        processed_response = {"status": original.get("status", "success")}
        
        # Reconstruct response from processed items
        for item in items:
            if item.source.endswith(".error"):
                processed_response["error"] = item.content
            elif item.source.endswith(".main_content"):
                processed_response["main_content"] = item.content
            elif item.source.endswith(".title"):
                # Remove "Title: " prefix if present
                title = item.content
                if title.startswith("Title: "):
                    title = title[7:]
                processed_response["title"] = title
            elif item.source.endswith(".description"):
                # Remove "Description: " prefix if present
                description = item.content
                if description.startswith("Description: "):
                    description = description[13:]
                processed_response["description"] = description
            elif item.source.endswith(".data"):
                try:
                    if item.content_type == "json":
                        processed_response["data"] = json.loads(item.content)
                    else:
                        processed_response["data"] = item.content
                except json.JSONDecodeError:
                    processed_response["data"] = item.content
            elif item.source.endswith(".text"):
                processed_response["text"] = item.content
            elif item.source.endswith(".result"):
                try:
                    if item.content_type == "json":
                        processed_response["result"] = json.loads(item.content)
                    else:
                        processed_response["result"] = item.content
                except json.JSONDecodeError:
                    processed_response["result"] = item.content
            elif item.source.endswith(".headers"):
                try:
                    processed_response["headers"] = json.loads(item.content)
                except json.JSONDecodeError:
                    processed_response["headers"] = {}
            elif item.source.endswith(".links"):
                # Preserve links field from browser tools
                try:
                    if item.content_type == "json":
                        processed_response["links"] = json.loads(item.content)
                    else:
                        processed_response["links"] = item.content
                except json.JSONDecodeError:
                    processed_response["links"] = item.content
            elif item.source.endswith(".images"):
                # Preserve images field from browser tools
                try:
                    if item.content_type == "json":
                        processed_response["images"] = json.loads(item.content)
                    else:
                        processed_response["images"] = item.content
                except json.JSONDecodeError:
                    processed_response["images"] = item.content
            else:
                # Handle generic fields (e.g., "services", "message", "total_count")
                # Pattern: tool_name.{field_name}
                if "." in item.source:
                    _, field_name = parse_qualified_name(item.source)
                    # Skip if it's a known pattern we already handled
                    known_patterns = [".error", ".main_content", ".title", ".description", ".data", 
                                     ".text", ".result", ".headers", ".links", ".images", ".status"]
                    if not any(item.source.endswith(p) for p in known_patterns):
                        content = item.content
                        
                        # If content_type is JSON, parse it directly
                        if item.content_type == "json":
                            try:
                                processed_response[field_name] = json.loads(content)
                            except (json.JSONDecodeError, ValueError):
                                processed_response[field_name] = content
                        else:
                            # Check if content is in "key: value" format (legacy)
                            if ":" in content and not content.strip().startswith(("{", "[")):
                                parts = content.split(":", 1)
                                if len(parts) == 2 and parts[0].strip() == field_name:
                                    # Extract the value part
                                    value_str = parts[1].strip()
                                    # Try to parse as JSON (might be a JSON string)
                                    try:
                                        processed_response[field_name] = json.loads(value_str)
                                    except (json.JSONDecodeError, ValueError):
                                        processed_response[field_name] = value_str
                                else:
                                    processed_response[field_name] = content
                            else:
                                # Try to parse as JSON (might be a JSON string)
                                try:
                                    processed_response[field_name] = json.loads(content)
                                except (json.JSONDecodeError, ValueError):
                                    processed_response[field_name] = content
        
        # Add processing metadata
        processed_response["_context_processed"] = True
        processed_response["_context_items"] = len(items)
        
        return processed_response
    
    def _estimate_tokens(self, content: str) -> int:
        """Estimate token count for content."""
        return max(1, len(content) // 4)


__all__ = ["ContextManager", "ContextRequirement", "ContextPriority", "ContextStrategy", "ContextItem"]
