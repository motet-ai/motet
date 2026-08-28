"""
Motet - Artifact RAG Evaluation Scaffold

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-05-07

Description:
    Lightweight retrieval evaluation scaffold for ADR-0110 artifact preparation.
    It loads a JSONL corpus and scores retrieved chunks by expected strategy,
    chunk kind, and coordinates so hybrid ranking can be tuned with repeatable
    fixtures as new strategies are added.

Dependencies:
    - json for corpus loading
    - pathlib for fixture path handling

Usage:
    python tests/evaluation/artifact_rag_evaluator.py tests/evaluation/artifact_rag_corpus.jsonl

Notes:
    - This module intentionally does not spin up Redis or embeddings. Test suites
      can import `score_retrieval_case` after supplying retrieved chunk dumps.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable


def load_corpus(path: str | Path) -> list[dict[str, Any]]:
    """Load artifact RAG evaluation cases from JSONL."""

    cases: list[dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped:
            cases.append(json.loads(stripped))
    return cases


def _coordinate_matches(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    for key, value in expected.items():
        actual_value = actual.get(key)
        if isinstance(value, list):
            if list(actual_value or []) != value:
                return False
        elif actual_value != value:
            return False
    return True


def score_retrieval_case(case: dict[str, Any], retrieved_chunks: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Score one case against retrieved chunk dictionaries."""

    expected_strategy = case.get("expected_strategy_id")
    expected_kind = case.get("expected_chunk_kind")
    expected_coordinates = case.get("expected_coordinates") or {}
    best_rank: int | None = None
    for rank, chunk in enumerate(retrieved_chunks, start=1):
        if expected_strategy and chunk.get("prep_strategy_id") != expected_strategy:
            continue
        if expected_kind and chunk.get("chunk_kind") != expected_kind:
            continue
        if expected_coordinates and not _coordinate_matches(expected_coordinates, chunk.get("coordinates") or {}):
            continue
        best_rank = rank
        break
    return {
        "query": case.get("query"),
        "matched": best_rank is not None,
        "best_rank": best_rank,
        "expected_strategy_id": expected_strategy,
        "expected_chunk_kind": expected_kind,
    }


def main() -> None:
    """Print a compact corpus summary."""

    import argparse

    parser = argparse.ArgumentParser(description="Summarize artifact RAG evaluation corpus")
    parser.add_argument("corpus", nargs="?", default="tests/evaluation/artifact_rag_corpus.jsonl")
    args = parser.parse_args()
    cases = load_corpus(args.corpus)
    print(json.dumps({"case_count": len(cases), "queries": [case.get("query") for case in cases]}, indent=2))


if __name__ == "__main__":
    main()

