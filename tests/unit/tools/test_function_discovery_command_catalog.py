"""
Motet - Function discovery command catalog quality tests

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-22

Description:
    Regression coverage for #194: every registered core command must carry a
    first-class `CommandRegistration.description` and index with usable discovery
    text for `core.help` hybrid search. Builds each command via
    `_build_command_item` after `DistributedCommand._ensure_commands_registered()`
    and asserts registry + searchable-content quality floors.

Dependencies:
    - motet.core.commands.distributed: loads the built-in command catalog
    - motet.core.commands.command_type_registry: registered command types
    - motet.core.tools.function_discovery_vector_store: indexing helpers under test
    - pytest: test runner

Usage:
    pytest tests/unit/tools/test_function_discovery_command_catalog.py -v

Notes:
    - Does not require Valkey or embeddings.
    - Covers registry descriptions, `_build_command_item`, keyword-index
      searchability, and the `core.help` recommendation path (via keyword-half
      stand-in for hybrid search).
    - Asserts the registry contract (`registration.description`) and the indexed
      entry; fails if either still uses the DecoratedCommand placeholder or thin
      "Data payload ..." data-class prose.
"""

from __future__ import annotations

import importlib
from typing import Any, Dict, List, Optional, Tuple

import pytest

from motet.core.commands.command_type_registry import command_type_registry
from motet.core.commands.distributed import DistributedCommand
from motet.core.tools.builtin import help as help_mod
from motet.core.tools.function_discovery_vector_store import FunctionDiscoveryVectorStore

# Built-in authoring docs are short but intent-bearing (shortest today ~30 chars).
_MIN_DESCRIPTION_CHARS = 20

# Beyond the normalized command-type tokens, indexed content should carry the
# description (and usually field names). Floor is description length alone.
_MIN_CONTENT_CHARS = 40

_PLACEHOLDER = "Dynamically generated command from decorated function."

# Modules whose import side effect registers built-in commands. Mirrored from
# DistributedCommand._ensure_commands_registered(); reload when another unit test
# has cleared the global CommandTypeRegistry (import alone will not re-decorate).
_BUILTIN_COMMAND_MODULES = (
    "motet.core.commands.builtin.agents",
    "motet.core.commands.builtin.artifacts",
    "motet.core.commands.builtin.conversation",
    "motet.core.commands.builtin.conversation_analysis",
    "motet.core.commands.builtin.conversation_analysis.complexity_analysis",
    "motet.core.commands.builtin.conversation_analysis.context_analysis",
    "motet.core.commands.builtin.conversation_analysis.conversation_analysis",
    "motet.core.commands.builtin.conversation_analysis.intent_analysis",
    "motet.core.commands.builtin.conversation_analysis.tone_analysis",
    "motet.core.commands.builtin.conversation_analysis.user_profile_analysis",
    "motet.core.commands.builtin.derivation",
    "motet.core.commands.builtin.memory",
    "motet.core.commands.builtin.model",
    "motet.core.commands.builtin.rag",
    "motet.core.commands.builtin.schedule",
    "motet.core.commands.builtin.test_decorator_command",
    "motet.core.commands.builtin.tool",
    "motet.core.commands.builtin.transform",
    "motet.core.commands.builtin.worker_lifecycle",
    "motet.core.commands.builtin.workflow",
    "motet.core.commands.builtin.sync_user_workflow",
    "motet.core.commands.concurrency",
    "motet.core.orchestration.turn.phases",
    "motet.core.orchestration.turn.agent_turn",
    "motet.core.orchestration.turn.resume_agent_turn",
    "motet.core.reasoning.react",
    "motet.core.bundles.deploy",
    "motet.core.bundles.bundle_reload",
)


def _store() -> FunctionDiscoveryVectorStore:
    store = FunctionDiscoveryVectorStore.__new__(FunctionDiscoveryVectorStore)
    store._initialized = True
    store._index_version = 0
    store._id_to_entry = {}
    store._removed_doc_ids = set()
    store._keyword_index_cache = None
    return store


def _core_registrations() -> Dict[str, Any]:
    return {
        str(ct): reg
        for ct, reg in command_type_registry.get_all_registrations().items()
        if str(ct).startswith("core.")
    }


def _index_core_commands(
    store: FunctionDiscoveryVectorStore,
    registrations: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    """
    Populate ``store._id_to_entry`` with every core command entry.

    Returns ``{command_type: description}`` for assertions. This is the same
    entry shape written into Valkey during worker indexing; the keyword half of
    hybrid search (and therefore help) reads descriptions from these entries.
    """
    regs = registrations if registrations is not None else _ensure_full_builtin_catalog()
    descriptions: Dict[str, str] = {}
    for command_type in regs:
        doc_id, item, entry = store._build_command_item(command_type)
        assert item is not None and entry is not None, command_type
        store._id_to_entry[doc_id] = entry
        descriptions[command_type] = str(entry.get("description") or "")
    store._index_version = len(store._id_to_entry)
    store._keyword_index_cache = None
    return descriptions


def _keyword_command_ranks(
    store: FunctionDiscoveryVectorStore, query: str
) -> List[Tuple[float, str]]:
    """Rank indexed commands with the production keyword half used by help."""
    expanded = FunctionDiscoveryVectorStore._expand_with_synonyms(
        list(FunctionDiscoveryVectorStore._tokenize_meaningful(query))
    )
    ranked: List[Tuple[float, str]] = []
    for doc_id, score in store._rank_by_keywords(expanded):
        entry = store._id_to_entry.get(doc_id) or {}
        if entry.get("type") != "command":
            continue
        ranked.append((score, str(entry.get("command_type") or "")))
    return ranked


def _keyword_only_search_functions(store: FunctionDiscoveryVectorStore):
    """Stand-in for hybrid ``search_functions`` using the keyword half + entry text."""

    def search_functions(
        query: str,
        top_k: int = 10,
        *,
        enable_boosting: bool = True,
        conversation_history: Any = None,
        search_types: Any = None,
    ) -> List[Dict[str, Any]]:
        del enable_boosting, conversation_history, search_types
        items: List[Dict[str, Any]] = []
        for score, command_type in _keyword_command_ranks(store, query)[: top_k * 2]:
            entry = next(
                (
                    e
                    for e in store._id_to_entry.values()
                    if e.get("command_type") == command_type
                ),
                {},
            )
            items.append(
                {
                    "type": "command",
                    "name": command_type,
                    "command_type": command_type,
                    "description": entry.get("description", ""),
                    "metadata": dict(entry),
                    "similarity_score": float(score),
                }
            )
        return items[:top_k]

    return search_functions


def _ensure_full_builtin_catalog() -> Dict[str, Any]:
    """
    Return the built-in ``core.*`` command registrations.

    Other unit tests may ``command_type_registry.clear()``; ``_ensure_commands_registered``
    only imports modules and will not re-run decorators. Reload when the core
    catalog looks empty/sparse.
    """
    DistributedCommand._ensure_commands_registered()
    registrations = _core_registrations()
    if len(registrations) >= 50:
        return registrations

    for module_name in _BUILTIN_COMMAND_MODULES:
        try:
            module = importlib.import_module(module_name)
            importlib.reload(module)
        except Exception:
            # Best-effort reload; missing optional modules should not hide
            # registration gaps in the assertion below.
            continue

    DistributedCommand._ensure_commands_registered()
    return _core_registrations()


def _catalog_failures() -> Tuple[List[str], Dict[str, Dict[str, Any]]]:
    """Return (failure messages, per-command summary) for the live registry."""
    registrations = _ensure_full_builtin_catalog()
    assert registrations, "expected built-in commands to be registered"

    store = _store()
    failures: List[str] = []
    summary: Dict[str, Dict[str, Any]] = {}

    for command_type in sorted(str(ct) for ct in registrations.keys()):
        registration = registrations[command_type]
        registry_description = str(getattr(registration, "description", "") or "").strip()
        if not registry_description:
            failures.append(f"{command_type}: empty CommandRegistration.description")
        elif registry_description == _PLACEHOLDER:
            failures.append(
                f"{command_type}: placeholder DecoratedCommand docstring on registration"
            )
        elif registry_description.lower().startswith("data payload"):
            failures.append(
                f"{command_type}: thin data-class docstring on registration "
                f"({registry_description!r})"
            )

        doc_id, item, entry = store._build_command_item(command_type)
        if item is None or entry is None:
            failures.append(f"{command_type}: _build_command_item returned None")
            continue

        description = str(entry.get("description") or "").strip()
        content = str(getattr(item, "content", "") or "").strip()
        summary[command_type] = {
            "description": description,
            "description_len": len(description),
            "content_len": len(content),
            "doc_id": doc_id,
        }

        if not description:
            failures.append(f"{command_type}: empty indexed description")
            continue
        if description == _PLACEHOLDER:
            failures.append(
                f"{command_type}: placeholder DecoratedCommand docstring still indexed"
            )
        if description.lower().startswith("data payload"):
            failures.append(
                f"{command_type}: thin data-class docstring used as indexed description "
                f"({description!r})"
            )
        if registry_description and description != registry_description[
            : FunctionDiscoveryVectorStore._ENTRY_DESCRIPTION_MAX_CHARS
        ]:
            failures.append(
                f"{command_type}: indexed description diverges from registration "
                f"({description!r} vs {registry_description!r})"
            )
        if len(description) < _MIN_DESCRIPTION_CHARS:
            failures.append(
                f"{command_type}: description too short "
                f"({len(description)} < {_MIN_DESCRIPTION_CHARS}): {description!r}"
            )
        if description not in content:
            failures.append(
                f"{command_type}: description missing from searchable content"
            )
        if len(content) < _MIN_CONTENT_CHARS:
            failures.append(
                f"{command_type}: searchable content too short "
                f"({len(content)} < {_MIN_CONTENT_CHARS})"
            )

        # Normalized command type alone is not enough for keyword discrimination.
        normalized_ct = command_type.replace("_", " ")
        remainder = content.replace(normalized_ct, "", 1).strip()
        if len(remainder) < _MIN_DESCRIPTION_CHARS:
            failures.append(
                f"{command_type}: content is little more than the command type name "
                f"(remainder={remainder!r})"
            )

    return failures, summary


def test_all_registered_commands_index_with_usable_discovery_text() -> None:
    """
    Every registered command must produce non-placeholder, non-trivial index text.

    This is the catalog-level guard for #194: help/search should not fall back to
    command-type name matching because descriptions are empty or generic.
    """
    failures, summary = _catalog_failures()

    assert len(summary) >= 50, (
        f"expected a full built-in catalog, got {len(summary)} commands"
    )
    assert not failures, (
        "command discovery index quality failures "
        f"({len(failures)}/{len(summary)}):\n- " + "\n- ".join(failures)
    )


def test_command_catalog_descriptions_are_mostly_unique() -> None:
    """
    Duplicate descriptions usually mean a shared placeholder slipped through.

    Allow a small amount of intentional overlap, but fail hard if many commands
    collapse onto the same prose.
    """
    _failures, summary = _catalog_failures()
    by_desc: Dict[str, List[str]] = {}
    for command_type, info in summary.items():
        by_desc.setdefault(info["description"], []).append(command_type)

    duplicates = {
        desc: types for desc, types in by_desc.items() if len(types) > 1
    }
    # A few intentional siblings may share wording; a widespread collapse is a bug.
    collapsed = {desc: types for desc, types in duplicates.items() if len(types) >= 5}
    assert not collapsed, (
        "too many commands share identical discovery descriptions:\n"
        + "\n".join(f"- ({len(types)}) {desc!r}: {types}" for desc, types in collapsed.items())
    )


@pytest.mark.parametrize(
    "command_type,needle",
    [
        ("core.memory_store", "tenant-isolated memory"),
        ("core.schedule", "schedule"),
        ("core.tool_execution", "tool"),
    ],
)
def test_representative_commands_keep_intent_bearing_descriptions(
    command_type: str,
    needle: str,
) -> None:
    _ensure_full_builtin_catalog()
    store = _store()
    _doc_id, item, entry = store._build_command_item(command_type)

    assert entry is not None and item is not None
    description = entry["description"].lower()
    assert needle in description, (
        f"{command_type} description should mention {needle!r}: {entry['description']!r}"
    )
    assert entry["description"] in item.content


def test_indexed_command_descriptions_are_in_keyword_searchable_text() -> None:
    """
    After indexing into ``_id_to_entry``, each command description must be part of
    the keyword-half corpus text that help's hybrid search matches against.
    """
    store = _store()
    descriptions = _index_core_commands(store)
    assert len(descriptions) >= 50

    keyword_index = store._get_keyword_index()
    assert keyword_index.n_docs >= 50

    missing: List[str] = []
    for command_type, description in descriptions.items():
        entry = next(
            e
            for e in store._id_to_entry.values()
            if e.get("command_type") == command_type
        )
        searchable = store._entry_searchable_text(entry)
        if description.lower() not in searchable:
            missing.append(command_type)
            continue
        # At least one discriminative token from the description must be indexed.
        desc_tokens = set(
            FunctionDiscoveryVectorStore._tokenize_meaningful(description)
        )
        doc_id = next(
            did for did, e in store._id_to_entry.items() if e.get("command_type") == command_type
        )
        indexed_tokens = keyword_index.doc_tokens.get(doc_id) or set()
        if not (desc_tokens & indexed_tokens):
            missing.append(f"{command_type}: no description tokens in keyword index")

    assert not missing, (
        "command descriptions missing from keyword-searchable index:\n- "
        + "\n- ".join(missing)
    )


@pytest.mark.parametrize(
    "query,expected_command",
    [
        (
            "store a note in tenant-isolated memory with tags",
            "core.memory_store",
        ),
        (
            "schedule a distributed command to run later or on a cron",
            "core.schedule",
        ),
        (
            "execute a registered tool by name with parameters",
            "core.tool_execution",
        ),
        (
            "list available agents for this tenant",
            "core.agent_list",
        ),
    ],
)
def test_keyword_half_ranks_commands_by_indexed_description(
    query: str,
    expected_command: str,
) -> None:
    """
    Paraphrase queries should surface the right command when only the keyword
    half of hybrid search runs over the indexed command catalog.
    """
    store = _store()
    descriptions = _index_core_commands(store)
    assert expected_command in descriptions

    ranked = _keyword_command_ranks(store, query)
    assert ranked, f"no keyword hits for query={query!r}"
    top_types = [ct for _score, ct in ranked[:15]]
    assert expected_command in top_types, (
        f"expected {expected_command!r} in top keyword ranks for {query!r}; "
        f"got {top_types[:10]}"
    )


@pytest.mark.parametrize(
    "query,expected_command,needle",
    [
        (
            "store a note in tenant-isolated memory with tags",
            "core.memory_store",
            "tenant-isolated",
        ),
        (
            "schedule a distributed command to run later or on a cron",
            "core.schedule",
            "schedule",
        ),
        (
            "execute a registered tool by name with parameters",
            "core.tool_execution",
            "tool",
        ),
    ],
)
def test_help_search_returns_indexed_command_descriptions(
    monkeypatch: pytest.MonkeyPatch,
    query: str,
    expected_command: str,
    needle: str,
) -> None:
    """
    Exercise the help recommendation path: indexed command entries must flow
    through ``search_functions`` into help recs with the registration description.
    """
    store = _store()
    descriptions = _index_core_commands(store)
    assert expected_command in descriptions

    store.search_functions = _keyword_only_search_functions(store)  # type: ignore[method-assign]
    monkeypatch.setattr(help_mod, "_get_vector_store", lambda: store)

    recs = help_mod._search_with_vector_store(
        query=query,
        limit=10,
        include_tools=False,
        include_commands=True,
        include_workflows=False,
    )
    assert recs, f"help returned no command recommendations for {query!r}"
    assert all(r.get("kind") == "command" for r in recs)

    by_type = {r.get("command_type"): r for r in recs}
    assert expected_command in by_type, (
        f"help did not surface {expected_command!r} for {query!r}; "
        f"got {list(by_type)[:10]}"
    )
    description = str(by_type[expected_command].get("description") or "")
    assert needle in description.lower(), (
        f"help rec for {expected_command} missing indexed description needle "
        f"{needle!r}: {description!r}"
    )
    assert description == descriptions[expected_command], (
        "help must return the same description stored on the indexed entry"
    )
