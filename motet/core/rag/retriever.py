"""
Motet - Artifact RAG Retriever

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Coordinates query-time artifact RAG retrieval for. It embeds the
    current user query, searches tenant-scoped artifact chunks, performs
    application-layer vector/keyword fusion, applies score and budget filters,
    and formats citation-ready context text for the orchestration context
    pipeline.

Dependencies:
    - ArtifactChunkRepository for Valkey Search access
    - ArtifactRagSelection and ArtifactRetrievalScope for structured results

Usage:
    retriever = ArtifactRagRetriever(repository=repository, embedding_fn=embedding_service.embed)
    selection = retriever.retrieve(query_text="What does the PDF say?", tenant_id="tenant", ...)

Notes:
    - Token budgeting uses a lightweight character approximation to avoid
      adding tokenizer dependencies to the worker image.
    - Hybrid retrieval intentionally runs in application code so the MVP remains
      compatible with Valkey Search runtimes that support VECTOR/TAG/NUMERIC but
      not native TEXT fields.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, Optional

from .repository import ArtifactChunkRepository
from .types import ArtifactChunkSearchResult, ArtifactRagSelection, ArtifactRetrievalScope

_TOKEN_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]*")
_HEADING_PREFIX_RE = re.compile(r"^\s*(?:#{1,6}\s+|(?:section|article|chapter|part)\s+[\w.-]+[:.)]?\s+|\d+(?:\.\d+)*[:.)]?\s+)", re.IGNORECASE)
_STOPWORDS = {
    "a",
    "about",
    "above",
    "after",
    "again",
    "against",
    "all",
    "also",
    "am",
    "an",
    "and",
    "any",
    "are",
    "around",
    "as",
    "ask",
    "at",
    "be",
    "because",
    "been",
    "before",
    "being",
    "below",
    "between",
    "both",
    "but",
    "by",
    "can",
    "could",
    "did",
    "do",
    "does",
    "doing",
    "down",
    "during",
    "each",
    "few",
    "for",
    "from",
    "further",
    "get",
    "give",
    "had",
    "has",
    "have",
    "having",
    "he",
    "her",
    "here",
    "hers",
    "him",
    "his",
    "how",
    "i",
    "if",
    "in",
    "into",
    "is",
    "it",
    "its",
    "me",
    "more",
    "most",
    "my",
    "no",
    "nor",
    "not",
    "of",
    "off",
    "on",
    "once",
    "only",
    "or",
    "other",
    "our",
    "ours",
    "out",
    "over",
    "own",
    "please",
    "show",
    "should",
    "so",
    "some",
    "such",
    "that",
    "the",
    "their",
    "theirs",
    "them",
    "then",
    "there",
    "these",
    "they",
    "this",
    "those",
    "through",
    "to",
    "under",
    "until",
    "up",
    "us",
    "use",
    "was",
    "we",
    "were",
    "what",
    "when",
    "where",
    "which",
    "while",
    "who",
    "whom",
    "why",
    "will",
    "with",
    "would",
    "you",
    "your",
    "yours",
}


class ArtifactRagRetriever:
    """Query-time artifact chunk retrieval and formatting."""

    def __init__(
        self,
        *,
        repository: ArtifactChunkRepository,
        embedding_fn: Any,
    ) -> None:
        self._repository = repository
        self._embedding_fn = embedding_fn

    @staticmethod
    def _approx_tokens(text: str) -> int:
        return max(1, (len(text or "") + 3) // 4)

    @staticmethod
    def _terms(text: str) -> list[str]:
        return [
            term
            for term in _TOKEN_RE.findall((text or "").lower())
            if len(term) > 1 and term not in _STOPWORDS
        ]

    @classmethod
    def _meaningful_query_terms(cls, query_text: str) -> list[str]:
        """Return de-duplicated query terms that should influence lexical rank."""

        return list(dict.fromkeys(cls._terms(query_text)))

    @classmethod
    def _query_phrases(cls, query_text: str) -> list[str]:
        """Return contiguous meaningful phrases worth exact-match reranking."""

        terms = cls._meaningful_query_terms(query_text)
        if len(terms) < 2:
            return []
        phrases = [" ".join(terms)]
        phrases.extend(f"{left} {right}" for left, right in zip(terms, terms[1:]))
        return list(dict.fromkeys(phrases))

    @staticmethod
    def _candidate_headings(text: str) -> list[str]:
        """Extract likely section headings without relying on source-specific metadata."""

        headings: list[str] = []
        for raw_line in (text or "").splitlines()[:12]:
            line = raw_line.strip()
            if not line or len(line) > 140:
                continue
            words = _TOKEN_RE.findall(line)
            uppercase_letters = sum(1 for char in line if char.isupper())
            letters = sum(1 for char in line if char.isalpha())
            has_heading_prefix = bool(_HEADING_PREFIX_RE.match(line))
            is_all_caps_heading = letters > 0 and uppercase_letters / letters >= 0.65
            is_short_title = (
                len(words) <= 10
                and len(line) <= 90
                and not line.endswith((".", "?", "!"))
                and bool(words)
            )
            if has_heading_prefix or is_all_caps_heading or is_short_title:
                headings.append(line)
        return headings

    @classmethod
    def lexical_score(cls, query_text: str, chunk: ArtifactChunkSearchResult) -> float:
        """Return a lightweight keyword/phrase score for one chunk."""

        query_terms = cls._meaningful_query_terms(query_text)
        if not query_terms:
            return 0.0
        query_counts = Counter(query_terms)
        haystack = f"{chunk.filename or ''}\n{chunk.content_text or ''}".lower()
        doc_terms = Counter(cls._terms(haystack))
        matched = sum(min(count, doc_terms.get(term, 0)) for term, count in query_counts.items())
        term_score = matched / max(1, sum(query_counts.values()))

        query_phrase = " ".join(query_terms)
        phrase_score = 0.0
        if query_phrase and query_phrase in haystack:
            phrase_score = 0.35
        else:
            bigrams = [phrase for phrase in cls._query_phrases(query_text) if phrase != query_phrase]
            if bigrams:
                phrase_score = 0.2 * (sum(1 for phrase in bigrams if phrase in haystack) / len(bigrams))

        filename_score = 0.1 if chunk.filename and any(term in chunk.filename.lower() for term in query_terms) else 0.0
        return min(1.0, term_score + phrase_score + filename_score)

    @classmethod
    def rerank_boost(cls, query_text: str, chunk: ArtifactChunkSearchResult) -> float:
        """Return generic deterministic boosts for likely section-title and exact phrase matches."""

        query_terms = cls._meaningful_query_terms(query_text)
        if not query_terms:
            return 0.0

        query_term_set = set(query_terms)
        boosts = 0.0
        haystack = f"{chunk.filename or ''}\n{chunk.content_text or ''}".lower()

        exact_phrase_hits = sum(1 for phrase in cls._query_phrases(query_text) if phrase in haystack)
        if exact_phrase_hits:
            boosts += min(0.12, 0.06 * exact_phrase_hits)

        best_heading_score = 0.0
        for heading in cls._candidate_headings(chunk.content_text):
            heading_terms = cls._meaningful_query_terms(heading)
            if not heading_terms:
                continue
            overlap = len(query_term_set.intersection(heading_terms)) / len(query_term_set)
            density = len(query_term_set.intersection(heading_terms)) / max(1, len(set(heading_terms)))
            best_heading_score = max(best_heading_score, overlap * density)

        if best_heading_score > 0.0:
            boosts += min(0.18, 0.18 * best_heading_score)

        return min(0.3, boosts)

    @staticmethod
    def _candidate_key(chunk: ArtifactChunkSearchResult) -> tuple[str, str, int]:
        return (chunk.source_artifact_id, chunk.prep_strategy_id, int(chunk.chunk_index))

    def _hybrid_candidates(
        self,
        *,
        query_text: str,
        vector_results: list[ArtifactChunkSearchResult],
        lexical_results: list[ArtifactChunkSearchResult],
        vector_weight: float,
        lexical_weight: float,
    ) -> list[ArtifactChunkSearchResult]:
        """Merge vector and lexical candidates with weighted normalized scores."""

        candidates: dict[tuple[str, str, int], ArtifactChunkSearchResult] = {}
        vector_ranks: dict[tuple[str, str, int], int] = {}
        lexical_ranks: dict[tuple[str, str, int], int] = {}

        for rank, chunk in enumerate(vector_results, start=1):
            key = self._candidate_key(chunk)
            candidates[key] = chunk.model_copy()
            vector_ranks[key] = rank

        scored_lexical: list[ArtifactChunkSearchResult] = []
        for chunk in lexical_results:
            chunk_copy = chunk.model_copy()
            chunk_copy.lexical_score = self.lexical_score(query_text, chunk_copy)
            if chunk_copy.lexical_score > 0.0:
                scored_lexical.append(chunk_copy)
        scored_lexical.sort(key=lambda item: (item.lexical_score, item.similarity), reverse=True)

        for rank, chunk in enumerate(scored_lexical, start=1):
            key = self._candidate_key(chunk)
            existing = candidates.get(key)
            if existing is None:
                candidates[key] = chunk
            else:
                existing.lexical_score = max(existing.lexical_score, chunk.lexical_score)
            lexical_ranks[key] = rank

        total_weight = max(0.0001, vector_weight + lexical_weight)
        normalized_vector_weight = max(0.0, vector_weight) / total_weight
        normalized_lexical_weight = max(0.0, lexical_weight) / total_weight
        fused: list[ArtifactChunkSearchResult] = []
        for key, chunk in candidates.items():
            chunk.lexical_score = max(chunk.lexical_score, self.lexical_score(query_text, chunk))
            boost = self.rerank_boost(query_text, chunk)
            rank_bonus = 0.0
            if key in vector_ranks:
                rank_bonus += 1.0 / (60.0 + vector_ranks[key])
            if key in lexical_ranks:
                rank_bonus += 1.0 / (60.0 + lexical_ranks[key])
            chunk.hybrid_score = (
                normalized_vector_weight * max(0.0, min(1.0, chunk.similarity))
                + normalized_lexical_weight * max(0.0, min(1.0, chunk.lexical_score))
                + rank_bonus
                + boost
            )
            fused.append(chunk)
        fused.sort(key=lambda item: (item.hybrid_score, item.similarity, item.lexical_score), reverse=True)
        return fused

    @staticmethod
    def _position_sort_key(chunk: ArtifactChunkSearchResult) -> tuple[str, float, int, str]:
        timestamp = 0.0
        coordinates = getattr(chunk, "coordinates", None)
        if coordinates is not None:
            raw_ts = getattr(coordinates, "timestamp_start", None)
            if raw_ts is not None:
                try:
                    timestamp = float(raw_ts)
                except (TypeError, ValueError):
                    timestamp = 0.0
        return (
            str(chunk.source_artifact_id or ""),
            timestamp,
            int(chunk.chunk_index or 0),
            str(chunk.chunk_kind or ""),
        )

    def _retrieve_position_ordered(
        self,
        *,
        tenant_id: str,
        motet_id: str,
        principal_id: Optional[str],
        role: str,
        conversation_id: Optional[str],
        scope: ArtifactRetrievalScope,
        artifact_ids: list[str],
        artifact_tags: Optional[list[str]],
        top_k: int,
        token_budget: int,
    ) -> ArtifactRagSelection:
        """Return artifact-scoped chunks in source position/timestamp order."""

        final_top_k = max(1, int(top_k or 5))
        max_tokens = max(1, int(token_budget or 4000))
        candidates = self._repository.list_scoped_chunks(
            tenant_id=tenant_id,
            motet_id=motet_id,
            principal_id=principal_id,
            role=role,
            conversation_id=conversation_id,
            scope=scope,
            artifact_ids=artifact_ids,
            artifact_tags=artifact_tags,
            max_candidates=max(final_top_k * 20, 200),
        )
        candidates.sort(key=self._position_sort_key)
        selected: list[ArtifactChunkSearchResult] = []
        context_parts: list[str] = []
        used_tokens = 0
        for result in candidates:
            formatted = self.format_chunk(result)
            chunk_tokens = self._approx_tokens(formatted)
            if used_tokens + chunk_tokens > max_tokens and selected:
                break
            if chunk_tokens > max_tokens:
                max_chars = max_tokens * 4
                formatted = formatted[:max_chars] + "\n[Chunk truncated to fit artifact RAG budget]"
                chunk_tokens = self._approx_tokens(formatted)
            selected.append(result)
            context_parts.append(formatted)
            used_tokens += chunk_tokens
            if len(selected) >= final_top_k:
                break

        context_text = ""
        if context_parts:
            context_text = (
                "Relevant artifact context. Cite sources using the Source metadata when answering.\n\n"
                + "\n\n---\n\n".join(context_parts)
            )
        return ArtifactRagSelection(chunks=selected, context_text=context_text, token_budget=max_tokens)

    @staticmethod
    def format_chunk(chunk: ArtifactChunkSearchResult) -> str:
        """Format one chunk with citation metadata."""

        filename = chunk.filename or chunk.source_artifact_id
        page_number = getattr(chunk.coordinates, "page_number", None) or getattr(chunk.coordinates, "page", None)
        page = f", page {page_number}" if page_number else ""
        heading_path = getattr(chunk.coordinates, "heading_path", None) or []
        heading_label = " > ".join(str(part) for part in heading_path if str(part).strip())
        section = f", section {heading_label}" if heading_label else ""
        table_range = getattr(chunk.coordinates, "range", None)
        table = f", table {table_range}" if table_range else ""
        return (
            f"[Source: {filename}{page}{section}{table}; source_artifact_id={chunk.source_artifact_id}; "
            f"derived_artifact_id={chunk.derived_artifact_id}; chunk={chunk.chunk_index}; "
            f"kind={chunk.chunk_kind}; strategy={chunk.prep_strategy_id}; similarity={chunk.similarity:.3f}]\n"
            f"{chunk.content_text}"
        )

    def retrieve(
        self,
        *,
        query_text: str,
        tenant_id: str,
        motet_id: str,
        principal_id: Optional[str],
        role: str,
        conversation_id: Optional[str],
        scope: ArtifactRetrievalScope = ArtifactRetrievalScope.CONVERSATION,
        artifact_ids: Optional[list[str]] = None,
        artifact_tags: Optional[list[str]] = None,
        top_k: int = 5,
        similarity_threshold: Optional[float] = None,
        token_budget: int = 4000,
        hybrid_enabled: bool = True,
        vector_weight: float = 0.7,
        lexical_weight: float = 0.3,
        candidate_multiplier: int = 4,
        position_ordered: bool = False,
    ) -> ArtifactRagSelection:
        """Retrieve and format artifact chunks for a user query."""

        query = (query_text or "").strip()
        if not query:
            return ArtifactRagSelection(chunks=[], context_text="", token_budget=token_budget)

        if position_ordered and artifact_ids:
            return self._retrieve_position_ordered(
                tenant_id=tenant_id,
                motet_id=motet_id,
                principal_id=principal_id,
                role=role,
                conversation_id=conversation_id,
                scope=scope,
                artifact_ids=artifact_ids,
                artifact_tags=artifact_tags,
                top_k=top_k,
                token_budget=token_budget,
            )

        raw_embedding = self._embedding_fn(query)
        query_embedding = raw_embedding.tolist() if hasattr(raw_embedding, "tolist") else list(raw_embedding)
        final_top_k = max(1, int(top_k or 5))
        vector_top_k = final_top_k
        if hybrid_enabled:
            vector_top_k = max(final_top_k, final_top_k * max(1, int(candidate_multiplier or 4)))
        results = self._repository.search(
            query_embedding=query_embedding,
            tenant_id=tenant_id,
            motet_id=motet_id,
            principal_id=principal_id,
            role=role,
            conversation_id=conversation_id,
            scope=scope,
            artifact_ids=artifact_ids,
            artifact_tags=artifact_tags,
            top_k=vector_top_k,
        )
        if hybrid_enabled:
            lexical_candidates = self._repository.list_scoped_chunks(
                tenant_id=tenant_id,
                motet_id=motet_id,
                principal_id=principal_id,
                role=role,
                conversation_id=conversation_id,
                scope=scope,
                artifact_ids=artifact_ids,
                artifact_tags=artifact_tags,
                max_candidates=max(vector_top_k, final_top_k * max(1, int(candidate_multiplier or 4))),
            )
            results = self._hybrid_candidates(
                query_text=query,
                vector_results=results,
                lexical_results=lexical_candidates,
                vector_weight=vector_weight,
                lexical_weight=lexical_weight,
            )
        else:
            for result in results:
                result.hybrid_score = result.similarity

        threshold = similarity_threshold if similarity_threshold is not None else 0.0
        selected: list[ArtifactChunkSearchResult] = []
        context_parts: list[str] = []
        used_tokens = 0
        max_tokens = max(1, int(token_budget or 4000))
        for result in results:
            if result.similarity < threshold:
                continue
            formatted = self.format_chunk(result)
            chunk_tokens = self._approx_tokens(formatted)
            if used_tokens + chunk_tokens > max_tokens and selected:
                break
            if chunk_tokens > max_tokens:
                max_chars = max_tokens * 4
                formatted = formatted[:max_chars] + "\n[Chunk truncated to fit artifact RAG budget]"
                chunk_tokens = self._approx_tokens(formatted)
            selected.append(result)
            context_parts.append(formatted)
            used_tokens += chunk_tokens
            if len(selected) >= final_top_k:
                break

        context_text = ""
        if context_parts:
            context_text = (
                "Relevant artifact context. Cite sources using the Source metadata when answering.\n\n"
                + "\n\n---\n\n".join(context_parts)
            )
        return ArtifactRagSelection(chunks=selected, context_text=context_text, token_budget=max_tokens)
