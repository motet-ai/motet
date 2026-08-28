"""
Motet - Tool Schema Exporter

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Export tool registry entries as CanonicalToolSchema.
    Converts Pydantic tool schemas to JSON Schema for native function calling.
    Includes caching, semantic pre-filtering, and MCP service grouping.

    Provider JSON (OpenAI / Anthropic / Gemini) is adapter-owned. This module
    does not sanitize names or render provider wire formats.

    Shows all context parameters as tokens instead of hiding them
    (USER_CONTEXT, SYSTEM, and CREDENTIAL params all use tokens for consistency).

    MCP Service Grouping: When any MCP tool from a service is selected, automatically
    includes all other tools from the same MCP service. This ensures related tools
    (e.g., search_gmail_messages when get_gmail_messages is selected) are available
    together while preserving relevance ranking.

Dependencies:
    - pydantic: Data validation and JSON Schema generation
    - structlog: Structured logging
    - typing: Type hints and annotations
    - Tool registry for accessing tool definitions
    - parameter_sources: For token generation and parameter classification

Usage:
    from motet.core.tools.schema_exporter import ToolSchemaExporter

    exporter = ToolSchemaExporter(tool_registry)
    tools = exporter.export_canonical(max_tools=20)

Notes:
    - Uses Pydantic's model_json_schema() for JSON Schema generation
    - Caches canonical schemas to avoid repeated conversions
    - Filters tools by relevance score if requested
    - Shows all context params (USER_CONTEXT, SYSTEM, CREDENTIAL) as tokens (__CTX_{key}__)
    - Tokens replaced with actual values at execution time (security via replacement)
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, cast
from pydantic import BaseModel
import structlog

from ..types import CanonicalToolSchema
from ..registry import RegistryProtocol

logger = structlog.get_logger(__name__)


def _schema_with_defs(result: Dict[str, Any], source: Dict[str, Any]) -> Dict[str, Any]:
    """
    Copy ``$defs`` / ``definitions`` from *source* onto *result* when present.

    Pydantic nests sub-models under ``$defs`` and references them via ``$ref``.
    Stripping defs while leaving refs makes provider schema validators fail
    (xAI: ``unresolvable $ref '#/$defs/ArtifactInput'``).
    """
    for key in ("$defs", "definitions"):
        defs = source.get(key)
        if isinstance(defs, dict) and defs:
            result[key] = defs
    return result


class ToolSchemaExporter:
    """
    Export tool registry entries as CanonicalToolSchema (ADR-0064 / ADR-0137).

    Provider JSON (OpenAI / Anthropic / Gemini) is adapter-owned. This class
    does not sanitize names or render provider wire formats.
    """
    
    def __init__(
        self,
        registry: RegistryProtocol[Any],
        function_discovery_store: Optional[Any] = None,
    ):
        """
        Initialize schema exporter.
        
        Args:
            registry: Registry-like instance implementing RegistryProtocol
            function_discovery_store: Optional FunctionDiscoveryVectorStore for semantic search (ADR-0051)
        """
        self.registry = registry
        self.function_discovery_store = function_discovery_store
        # Values are provider tool lists (List[Dict]) or canonical dumps (list of model_dump dicts)
        self._cache: Dict[str, Any] = {}
        self._cache_timestamps: Dict[str, float] = {}
        self._cache_ttl = 3600  # 1 hour default

    def export_canonical(
        self,
        *,
        max_tools: Optional[int] = None,
        filter_query: Optional[str] = None,
        preselected_tools: Optional[List[str]] = None,
        tool_audience: str = "agent",
    ) -> List[CanonicalToolSchema]:
        """
        Export tools in canonical provider-agnostic format (ADR-0064).

        This is the preferred export for adapter-based model calls. Provider adapters then map:
        - OpenAI Chat Completions: {"type":"function","function":{"name",...,"parameters"}}
        - OpenAI Responses: {"type":"function","name",...,"parameters"}
        - Anthropic: {"name","description","input_schema":...}

        Args:
            max_tools: Maximum number of tools to export
            filter_query: Optional query for relevance filtering (ADR-0051)
            preselected_tools: Optional explicit list from semantic search (ADR-0051)

        Returns:
            List[CanonicalToolSchema]
        """
        preselected_key = ",".join(sorted(preselected_tools)) if preselected_tools else None
        cache_key = f"canonical:{max_tools}:{filter_query}:{preselected_key}:{tool_audience}"

        if cache_key in self._cache:
            timestamp = self._cache_timestamps.get(cache_key, 0)
            if time.time() - timestamp < self._cache_ttl:
                cached = self._cache[cache_key]
                if isinstance(cached, list):
                    # Cache stores JSON-serializable objects; ensure we return CanonicalToolSchema objects
                    try:
                        out: List[CanonicalToolSchema] = []
                        for t in cached:
                            if isinstance(t, CanonicalToolSchema):
                                out.append(t)
                            else:
                                out.append(CanonicalToolSchema.model_validate(cast(Dict[str, Any], t)))
                        return out
                    except Exception:
                        # Fall through to recompute
                        pass

        internal_tools = self._get_internal_definitions(
            max_tools=max_tools,
            filter_query=filter_query,
            preselected_tools=preselected_tools,
            tool_audience=tool_audience,
        )

        exported: List[CanonicalToolSchema] = [
            CanonicalToolSchema(name=t["name"], description=t.get("description", "") or "", json_schema=t.get("parameters") or {})
            for t in internal_tools
            if isinstance(t, dict) and isinstance(t.get("name"), str)
        ]

        # Cache a list of dicts to avoid pydantic object issues in the cache store
        self._cache[cache_key] = [t.model_dump() for t in exported]
        self._cache_timestamps[cache_key] = time.time()
        return exported
    
    def _get_internal_definitions(
        self,
        max_tools: Optional[int],
        filter_query: Optional[str],
        preselected_tools: Optional[List[str]] = None,
        tool_audience: str = "agent",
    ) -> List[Dict[str, Any]]:
        """
        Get tools in internal standard format with relevance ranking.
        
        ADR-0051: Supports semantic search via FunctionDiscoveryVectorStore and
        preselected tools from semantic search.
        """
        tools = []
        
        # Get all tool names from registry
        listed = None
        # ADR-0075 hard cutover: prefer scope-aware visible tool listing when motet context exists.
        try:
            from motet.core.commands.decorator import get_motet_context
            from ..registry import ScopeFilter
            motet = get_motet_context()
            if (
                motet
                and hasattr(self.registry, "list_visible")
                and callable(getattr(self.registry, "list_visible"))
            ):
                listed = self.registry.list_visible(
                    ScopeFilter(
                        tenant_id=getattr(motet, "tenant_id", "") or "*",
                        motet_id=getattr(motet, "motet_id", "") or "*",
                        role="*",
                        principal_id=getattr(motet, "principal_id", "") or "*",
                    )
                )
        except Exception:
            listed = None

        if listed is None:
            listed = self.registry.list_items()

        if isinstance(listed, dict):
            tool_names = list(listed.keys())
        else:
            try:
                tool_names = list(listed or [])
            except Exception:
                tool_names = []
        
        # Use preselected tools if provided (from semantic search)
        if preselected_tools:
            # Filter to only tools that exist in registry
            available_tools = [name for name in preselected_tools if name in tool_names]
            missing_tools = [name for name in preselected_tools if name not in tool_names]
            if missing_tools:
                logger.warning(
                    "schema_export_preselected_tools_not_in_registry",
                    missing_tools=missing_tools,
                    note="These tools were preselected but are not registered. Workers may need to be restarted."
                )
            tool_names = available_tools
            # Apply max_tools limit
            if max_tools:
                tool_names = tool_names[:max_tools]
        elif filter_query:
            # ADR-0075 hard cutover: semantic discovery is required for filtered export.
            if not self.function_discovery_store:
                logger.warning(
                    "schema_export_semantic_search_unavailable",
                    query=filter_query,
                    note="No function discovery store configured; returning no filtered tools",
                )
                tool_names = []
            else:
                semantic_results = self.function_discovery_store.search_functions(
                    query=filter_query,
                    top_k=max_tools or 20
                )
                tool_names = [
                    result["name"] for result in semantic_results
                    if result["type"] == "tool" and result["name"] in tool_names
                ]
                logger.info(
                    "schema_export_semantic_search",
                    query=filter_query,
                    semantic_results=len(semantic_results),
                    tool_results=len(tool_names)
                )
        elif max_tools:
            # No query provided - use priority and limit
            tool_names = self._rank_tools_by_priority(tool_names)[:max_tools]
        
        # MCP Service Grouping is intentionally NOT applied here (ADR-0074 Rule 11).
        # The former _add_related_mcp_tools expansion included all tools from the same
        # MCP service whenever any one was selected. Removed because:
        #   1. The LLM in a ReAct loop plans one step at a time.
        #   2. Under ADR-0128, same-service follow-ons are discovered via
        #      tools_search → tool_call (observation tail), not by expanding tools[].
        #   3. Expanding to a full service would fill the max_tools budget and crowd out
        #      workflow schemas (ADR-0074 Rule 12).

        for tool_name in tool_names:
            tool_info = self.registry.get(tool_name)
            if not tool_info:
                continue
            if tool_audience == "agent" and not bool(getattr(tool_info, "expose_to_agents", True)):
                continue
            if tool_audience == "prep_planner" and getattr(tool_info, "prep_manifest", None) is None:
                continue
            
            # Extract JSON Schema from Pydantic model
            parameters_schema = self._extract_json_schema(tool_info.tool_schema)
            
            tools.append({
                "name": tool_name,
                "description": tool_info.description,
                "parameters": parameters_schema
            })
        
        return tools
    
    def _extract_json_schema(self, schema_or_model: Optional[Any]) -> Dict[str, Any]:
        """
        Extract JSON Schema from Pydantic model or MCP JSON Schema dict.
        
        Filters out parameters marked as system-injected (ADR-0046 Phase 2).
        
        For Pydantic tools:
        - Parameters with x-imf-hide-from-llm=true are removed
        
        For MCP tools:
        - Parameters matching PARAMETER_CONVENTIONS are removed
        - This prevents LLM from seeing parameters it can't fill
        - Filtered parameters are injected at execution time
        
        Examples of filtered parameters:
        - user_google_email, user_email (USER_CONTEXT)
        - access_token, api_key (CREDENTIAL)
        - task_id, conversation_id (SYSTEM)
        
        Args:
            schema_or_model: Either a Pydantic model class or MCP JSON Schema dict
        
        Returns:
            JSON Schema dict with filtered properties
        """
        if not schema_or_model:
            # No schema defined - return empty object schema
            return {
                "type": "object",
                "properties": {},
                "required": []
            }
        
        # Handle MCP JSON Schema (dict)
        if isinstance(schema_or_model, dict):
            # MCP tools provide JSON Schema directly
            # Extract from 'inputSchema' if present (MCP format)
            logger.debug(
                "schema_extraction_mcp_dict",
                has_inputSchema='inputSchema' in schema_or_model,
                top_level_keys=list(schema_or_model.keys())[:5]  # First 5 keys for debugging
            )
            
            if 'inputSchema' in schema_or_model:
                schema = schema_or_model['inputSchema']
            else:
                schema = schema_or_model
            
            # ADR-0046 Enhancement: Token-based context parameters for MCP tools
            # Import here to avoid circular dependency
            from .parameter_sources import PARAMETER_CONVENTIONS, get_context_token, ParameterSource
            
            properties = schema.get("properties", {})
            required = schema.get("required", [])
            
            # Filter or tokenize properties based on parameter conventions
            filtered_properties = {}
            filtered_required = []
            
            for prop_name, prop_schema in properties.items():
                # Check if this parameter matches a convention
                if prop_name in PARAMETER_CONVENTIONS:
                    source, context_key = PARAMETER_CONVENTIONS[prop_name]
                    
                    # All context params (USER_CONTEXT, SYSTEM, CREDENTIAL): Show as token
                    # Create a copy of the schema to avoid modifying original
                    tokenized_schema = prop_schema.copy() if isinstance(prop_schema, dict) else dict(prop_schema)
                    
                    # Generate token for this context key
                    token = get_context_token(context_key)
                    
                    # Set default value to token
                    tokenized_schema["default"] = token
                    
                    # Update description to indicate token usage
                    original_desc = tokenized_schema.get("description", "")
                    tokenized_schema["description"] = (
                        f"{original_desc} You must include this parameter with the token value: {token} "
                    )
                    
                    filtered_properties[prop_name] = tokenized_schema
                    
                    # Don't mark tokenized params as required (they're auto-provided)
                    
                    logger.debug(
                        "schema_tokenized_injected_param",
                        param_name=prop_name,
                        convention=(source.value, context_key),
                        token=token,
                        reason="tokenized"
                    )
                else:
                    # Keep this parameter for LLM (no convention match)
                    filtered_properties[prop_name] = prop_schema
                    if prop_name in required:
                        filtered_required.append(prop_name)
            
            logger.debug(
                "schema_extraction_mcp_filtered",
                original_param_count=len(properties),
                filtered_param_count=len(filtered_properties),
                removed_count=len(properties) - len(filtered_properties)
            )
            
            # Preserve $defs/definitions so nested $ref targets remain resolvable
            # (xAI/OpenAI Responses reject unresolvable refs; see workspace_shell_exec).
            return _schema_with_defs(
                {
                    "type": schema.get("type", "object"),
                    "properties": filtered_properties,
                    "required": filtered_required,
                },
                schema,
            )
        
        # Handle Pydantic model
        try:
            # Pydantic v2: Use model_json_schema()
            if hasattr(schema_or_model, 'model_json_schema'):
                schema = schema_or_model.model_json_schema()
            # Pydantic v1: Use schema()
            elif hasattr(schema_or_model, 'schema'):
                schema = schema_or_model.schema()
            else:
                logger.warning(
                    "schema_extraction_no_method",
                    model=schema_or_model.__name__
                )
                return {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            
            # Ensure required fields
            if "type" not in schema:
                schema["type"] = "object"
            if "properties" not in schema:
                schema["properties"] = {}
            
            # ADR-0046 Enhancement: Token-based context parameters
            # Filter or tokenize properties based on parameter source
            filtered_properties = {}
            filtered_required = []
            
            # Import here to avoid circular dependency
            from .parameter_sources import get_context_token, ParameterSource
            
            for prop_name, prop_schema in schema.get("properties", {}).items():
                # Check parameter metadata
                hide_from_llm = prop_schema.get("x-imf-hide-from-llm", False)
                use_token = prop_schema.get("x-imf-use-token", False)
                source_str = prop_schema.get("x-imf-source")
                context_key = prop_schema.get("x-imf-context-key", prop_name)
                
                if hide_from_llm:
                    # Legacy: Hide completely if explicitly marked (backward compatibility)
                    logger.debug(
                        "parameter_filtered_from_llm_schema",
                        model=schema_or_model.__name__,
                        parameter=prop_name,
                        reason="x-imf-hide-from-llm=true (legacy)"
                    )
                    continue
                
                if use_token and source_str:
                    # USER_CONTEXT, SYSTEM, or CREDENTIAL params: Show as token
                    # Create a copy of the schema to avoid modifying original
                    tokenized_schema = prop_schema.copy() if isinstance(prop_schema, dict) else dict(prop_schema)
                    
                    # Generate token for this context key
                    token = get_context_token(context_key)
                    
                    # Set default value to token
                    tokenized_schema["default"] = token
                    
                    # Update description to indicate token usage
                    original_desc = tokenized_schema.get("description", "")
                    tokenized_schema["description"] = (
                        f"{original_desc} You must include this parameter with the token value: {token} "
                    )
                    
                    # Mark as optional (token will be replaced at execution)
                    # Remove from required list if present
                    
                    filtered_properties[prop_name] = tokenized_schema
                    
                    # Don't mark tokenized params as required (they're auto-provided)
                    # But keep them in the schema so LLM sees them
                    
                    logger.debug(
                        "parameter_tokenized_in_llm_schema",
                        model=schema_or_model.__name__,
                        parameter=prop_name,
                        token=token,
                        source=source_str
                    )
                else:
                    # LLM_PROVIDED params: Keep as-is
                    filtered_properties[prop_name] = prop_schema
                    
                    # Include in required if marked as required
                    if prop_name in schema.get("required", []):
                        filtered_required.append(prop_name)
            
            # Preserve $defs/definitions so nested $ref targets remain resolvable
            # (xAI rejects schemas with $ref but missing $defs).
            return _schema_with_defs(
                {
                    "type": schema.get("type", "object"),
                    "properties": filtered_properties,
                    "required": filtered_required,
                },
                schema,
            )
            
        except Exception as e:
            logger.error(
                "schema_extraction_failed",
                model=schema_or_model.__name__ if hasattr(schema_or_model, '__name__') else type(schema_or_model).__name__,
                error=str(e),
                exc_info=True
            )
            return {
                "type": "object",
                "properties": {},
                "required": []
            }
    
    def _rank_tools_by_relevance(
        self,
        tool_names: List[str],
        query: str,
        max_tools: Optional[int]
    ) -> List[str]:
        """
        Rank tools by relevance to query using keyword matching and categories.
        
        Scoring system:
        - Exact name match: +100 points
        - Keyword in tool name: +50 points
        - Keyword in description: +20 points
        - Keyword in keywords list: +30 points
        - Category match: +40 points
        - Data type match: +25 points
        - Priority boost: +priority value
        
        Args:
            tool_names: List of tool names to rank
            query: User query to match against
            max_tools: Maximum tools to return
            
        Returns:
            List of tool names sorted by relevance score
        """
        import re
        
        # Normalize query for matching
        query_lower = query.lower()
        query_tokens = set(re.findall(r'\w+', query_lower))
        
        # Detect query intent patterns
        is_url = bool(re.search(r'https?://|www\.', query))
        is_math = bool(re.search(r'\d+\s*[\+\-\*\/\%]\s*\d+|calculate|compute', query_lower))
        is_search = any(word in query_lower for word in ['search', 'find', 'lookup', 'google'])
        is_memory = any(word in query_lower for word in ['remember', 'recall', 'memory', 'saved'])
        is_file = any(word in query_lower for word in ['file', 'read', 'write', 'document'])
        
        # Category priority map based on detected intent
        # Use UPDATE instead of REPLACE to support multiple intents
        category_priority = {}
        if is_url:
            # CRITICAL: URL detection gets VERY high priority to override keyword matches
            # Google Workspace tools can score 300+ from keywords alone, so HTTP must be much higher
            # Use 1000+ to absolutely guarantee HTTP tools win for URL queries
            category_priority.update({'http': 1000, 'web': 500, 'browser': 800, 'network': 600})
        if is_math:
            category_priority.update({'math': 150, 'calculator': 100})
        if is_search:
            category_priority.update({'search': 100, 'web': 60})
        if is_memory:
            category_priority.update({'memory': 100, 'storage': 60})
        if is_file:
            # Only boost file tools if NO URL is present (otherwise filesystem wins over HTTP)
            if not is_url:
                category_priority.update({'filesystem': 100, 'file': 80})
        
        scored_tools = []
        
        for tool_name in tool_names:
            tool_info = self.registry.get(tool_name)
            if not tool_info:
                continue
            
            score = 0
            
            # Name matching
            tool_name_lower = tool_name.lower()
            if tool_name_lower == query_lower:
                score += 100  # Exact match
            else:
                # Check each query token against tool name
                for token in query_tokens:
                    if len(token) > 2 and token in tool_name_lower:
                        score += 50
            
            # Description matching
            if tool_info.description:
                desc_lower = tool_info.description.lower()
                for token in query_tokens:
                    if len(token) > 2 and token in desc_lower:
                        score += 20
            
            # Keywords matching
            if tool_info.keywords:
                for keyword in tool_info.keywords:
                    if keyword.lower() in query_lower or any(
                        token in keyword.lower() for token in query_tokens if len(token) > 2
                    ):
                        score += 30
            
            # Category matching with intent-based priority
            if tool_info.category:
                category_lower = tool_info.category.lower()
                if category_lower in category_priority:
                    score += category_priority[category_lower]
                elif any(token in category_lower for token in query_tokens if len(token) > 2):
                    score += 40
            
            # Data types matching
            if tool_info.data_types:
                for data_type in tool_info.data_types:
                    if data_type.lower() in query_lower or any(
                        token in data_type.lower() for token in query_tokens if len(token) > 2
                    ):
                        score += 25
            
            # Priority boost (tools with lower priority value are more important)
            if tool_info.priority:
                # Invert priority: priority 1 = +10 points, priority 10 = +1 point
                score += max(0, 11 - tool_info.priority)
            
            scored_tools.append((tool_name, score))
        
        # Sort by score (descending)
        scored_tools.sort(key=lambda x: x[1], reverse=True)
        
        # Log top scoring tools for debugging
        if scored_tools:
            logger.info(
                "tool_relevance_ranking_complete",
                query_length=len(query),
                total_tools=len(scored_tools),
                top_3_scores=[(name, score) for name, score in scored_tools[:3]]
            )
        
        # Return top N tool names
        result = [name for name, score in scored_tools]
        if max_tools:
            result = result[:max_tools]
        
        return result
    
    def _add_related_mcp_tools(
        self,
        selected_tools: List[str],
        max_tools: Optional[int],
        filter_query: Optional[str]
    ) -> List[str]:
        """
        Add related MCP tools from the same service when any MCP tool is selected.
        
        If any MCP tool (format: mcp.service_id.tool_name) is in the selected tools,
        this method finds all other tools from the same service_id and adds them to
        the result, preserving relevance ranking.
        
        Args:
            selected_tools: List of tool names already selected (ranked by relevance)
            max_tools: Maximum number of tools to return (overall limit)
            filter_query: Optional query for re-ranking related tools by relevance
            
        Returns:
            List of tool names with related MCP tools added, respecting max_tools limit
        """
        if not selected_tools:
            return selected_tools
        
        # Track selected tools to avoid duplicates
        selected_set = set(selected_tools)
        
        # Identify MCP tools and extract their service_ids
        mcp_services = set()
        for tool_name in selected_tools:
            if tool_name.startswith("mcp."):
                # Extract service_id from format: mcp.service_id.tool_name
                parts = tool_name.split(".", 2)
                if len(parts) >= 2:
                    service_id = parts[1]
                    mcp_services.add(service_id)

        # If no MCP tools found, return original list
        if not mcp_services:
            return selected_tools

        # Find all tools from the same MCP services that aren't already selected
        all_tool_names = list(self.registry.list_items().keys())
        related_tools = []

        for tool_name in all_tool_names:
            # Skip if already selected
            if tool_name in selected_set:
                continue

            # Check if this tool belongs to one of the identified MCP services
            if tool_name.startswith("mcp."):
                parts = tool_name.split(".", 2)
                if len(parts) >= 2:
                    service_id = parts[1]
                    if service_id in mcp_services:
                        related_tools.append(tool_name)
        
        # If no related tools found, return original list
        if not related_tools:
            return selected_tools
        
        # Re-rank related tools by relevance if filter_query provided, otherwise by priority
        if filter_query:
            # Re-rank related tools by relevance to maintain consistency
            related_tools = self._rank_tools_by_relevance(related_tools, filter_query, None)
        else:
            # Rank by priority
            related_tools = self._rank_tools_by_priority(related_tools)
        
        # Combine selected tools with related tools (preserving order)
        # Selected tools come first (already ranked), then related tools
        combined_tools = selected_tools + related_tools
        
        # Respect max_tools limit if provided
        if max_tools:
            combined_tools = combined_tools[:max_tools]
        
        logger.info(
            "mcp_service_grouping_applied",
            selected_count=len(selected_tools),
            related_count=len(related_tools),
            services=list(mcp_services),
            final_count=len(combined_tools),
            max_tools=max_tools
        )
        
        return combined_tools
    
    def _rank_tools_by_priority(self, tool_names: List[str]) -> List[str]:
        """
        Rank tools by priority (lower priority number = higher precedence).
        
        Args:
            tool_names: List of tool names to rank
            
        Returns:
            List of tool names sorted by priority
        """
        tools_with_priority = []
        
        for tool_name in tool_names:
            tool_info = self.registry.get(tool_name)
            if not tool_info:
                continue
            
            # Use priority value (default to 10 if not set)
            priority = tool_info.priority if tool_info.priority else 10
            tools_with_priority.append((tool_name, priority))
        
        # Sort by priority (ascending - lower is better)
        tools_with_priority.sort(key=lambda x: x[1])
        
        return [name for name, priority in tools_with_priority]

    def clear_cache(self) -> None:
        """Clear the schema cache."""
        self._cache.clear()
        self._cache_timestamps.clear()
        logger.info("schema_cache_cleared")
    
    def set_cache_ttl(self, ttl_seconds: int) -> None:
        """
        Set cache TTL.
        
        Args:
            ttl_seconds: Cache time-to-live in seconds
        """
        self._cache_ttl = ttl_seconds
        logger.info("schema_cache_ttl_updated", ttl_seconds=ttl_seconds)

