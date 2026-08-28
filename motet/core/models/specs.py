"""
Motet - Model Specifications

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-25

Description:
    Model specifications, capability definitions, and model registry.
    Defines what capabilities each model supports, pricing configuration,
    provenance, optional public release dates, and metadata about models.
    Includes DeepSeek V4 Responses + Chat Completions specs, Claude Opus 5, Grok 4.6,
    and Meta Muse Spark.
    tags eligible hosted chat models with CAP_PROMPT_CACHING.

Dependencies:
    - pydantic: Data validation for model specifications
    - typing: Type hints for registry structure
    - datetime.date: Optional released_at on ModelSpec

Usage:
    from motet.core.models.specs import ModelSpec, CAP_STREAM, MODEL_REGISTRY
    
    # Check model capabilities
    spec = MODEL_REGISTRY["openai"]["gpt-4o-mini"]
    if CAP_STREAM in spec.capabilities:
        print("Model supports streaming")
    
    # Create new model spec
    custom_spec = ModelSpec(
        provider="custom",
        name="my-model",
        capabilities={CAP_STREAM, CAP_TOOL_USE},
        max_output_tokens=4096,
        released_at=date(2026, 1, 15),
    )

Notes:
    - Capabilities are defined as constants for type safety
    - MODEL_REGISTRY is the source of truth for available models
    - Specs are immutable (frozen Pydantic models)
    - released_at is the provider's public launch date (best-effort); None = unknown
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple

from pydantic import BaseModel, Field

from ..cost.pricing import ModelPricing


# Capability constants
CAP_STREAM = "stream"
CAP_VISION = "vision"
CAP_TOOL_USE = "tool_use"
CAP_JSON_MODE = "json_mode"
CAP_SYSTEM_PROMPT = "system_prompt"
CAP_REASONING = "reasoning"
# ADR-0113: model can generate/return images (image output), not just accept them as input
CAP_IMAGE_GENERATION = "image_generation"
# ADR-0114: model supports grammar/schema-constrained (guaranteed-parseable) structured
# output. On the local path this is GBNF-constrained decoding (compiled from JSON Schema);
# cloud analogs are response_format: json_schema / strict tools / responseSchema.
CAP_STRUCTURED_OUTPUT = "structured_output"
# ADR-0124: model participates in provider-side prompt caching (explicit breakpoints
# and/or automatic cache + optional prompt_cache_key affinity). Local / image-only
# models omit this; absence means caching policy is a no-op.
CAP_PROMPT_CACHING = "prompt_caching"


class ModelHosting(str, Enum):
    """Where inference runs — the data-egress fact.

    LOCAL models run on-prem / in-cluster, so request data never leaves the
    deployment's trust boundary. HOSTED_API models are served by a third party,
    so using them egresses data — a restriction concern independent of where the
    model was trained.
    """
    LOCAL = "local"
    HOSTED_API = "hosted_api"


class RestrictionTag(str, Enum):
    """Composable, normalized restriction-relevant attributes of a model.

    These are *facts*, not verdicts: each tag names a restriction concern the
    model carries. A deployment policy decides which tags are disqualifying (e.g.
    a defense profile forbids FOREIGN_ADVERSARY_ORIGIN; an air-gapped profile
    forbids DATA_EGRESS; an auditability profile forbids CLOSED_WEIGHTS). The gate
    that consumes these is a follow-on; this is storage only.

    This is the canonical machine surface for policy. The descriptive primitive
    fields on ``ModelProvenance`` (origin / open_weights / license / hosting) stay
    for humans/UI; tags are the normalized, enumerated form so a future gate keys
    on a stable vocabulary rather than re-deriving meaning from raw facts.
    """
    FOREIGN_ADVERSARY_ORIGIN = "foreign_adversary_origin"  # origin from a foreign-adversary nation (e.g. cn, ru, ir, kp)
    NON_US_ORIGIN = "non_us_origin"                          # any non-US origin (e.g. eu)
    DATA_EGRESS = "data_egress"                              # served via a third-party hosted API
    CLOSED_WEIGHTS = "closed_weights"                        # weights are not open / inspectable
    NON_COMMERCIAL_LICENSE = "non_commercial_license"        # license forbids commercial use
    EXPORT_CONTROLLED = "export_controlled"                  # subject to ITAR/EAR-style export control


class ModelProvenance(BaseModel):
    """
    Provenance facts about a model.

    Provenance is an *intrinsic fact* about a model (who trained it, under what
    license, where it is hosted, what restriction concerns it carries). Whether a
    given fact is *acceptable* for a deployment is a separate policy concern,
    enforced at routing/selection time — NOT in this registry. This model only
    records the facts; the gate is a follow-on.

    Pure facts only — no pre-baked acceptability verdict. Deployment policy
    composes acceptability from ``restrictions`` (forbid the tags it cares about).

    Attributes:
        origin: Region/country of the training organization (e.g. "us", "cn", "eu").
        vendor: Training organization / vendor (e.g. "Microsoft", "Google", "Alibaba").
        open_weights: Whether the model is distributed as open weights.
        license: License identifier (e.g. "MIT", "Gemma", "Apache-2.0", "Proprietary").
        hosting: Where inference runs (the data-egress fact); see ModelHosting.
        restrictions: Normalized set of restriction concerns the model carries
            (see RestrictionTag). Empty = no known restriction concerns.
        notes: Optional free-form provenance notes.
    """
    origin: str
    vendor: Optional[str] = None
    open_weights: bool = False
    license: Optional[str] = None
    hosting: ModelHosting = ModelHosting.HOSTED_API
    restrictions: FrozenSet[RestrictionTag] = Field(default_factory=frozenset)
    notes: Optional[str] = None

    model_config = {"frozen": True}


class ModelSpec(BaseModel):
    """
    Immutable specification for a model.
    
    Attributes:
        provider: Model provider name (e.g., "openai", "anthropic")
        name: Model identifier (e.g., "gpt-4o-mini")
        display_name: Optional user-friendly display name (e.g., "GPT-4o Mini")
        capabilities: Set of supported capabilities (e.g., CAP_STREAM, CAP_VISION)
        max_output_tokens: Maximum tokens the model can generate
        base_url: Optional API base URL for this model (enables same model from different hosts)
        provenance: Optional origin/licensing facts; None = unknown provenance
        released_at: Optional public launch date; None = unknown / not yet recorded
    """
    provider: str
    name: str
    display_name: Optional[str] = None  # User-friendly name for UI (e.g., "GPT-4o Mini")
    capabilities: Set[str]
    max_output_tokens: int

    # Public provider launch date (calendar day). Used for recency sorting (e.g. live
    # adapter canaries). Best-effort — aliases share the parent model's date.
    # Prefer setting this on the ModelSpec constructor when adding models; the
    # registry-wide map below backfills existing entries.
    released_at: Optional[date] = None

    # ADR-0064: ModelSpec-driven routing (model registry chooses adapter/backend)
    #
    # These fields are intentionally optional to support incremental rollout.
    # - If `default_adapter` is set and that adapter exists in adapter_registry, model commands should prefer it.
    # - `fallback_adapters` provide an ordered rollback chain (e.g., OpenAI responses -> chat_completions).
    default_adapter: Optional[str] = None
    fallback_adapters: Optional[list[str]] = None
    default_model_settings: Optional[Dict[str, Any]] = None

    # ADR-0064: ModelSpec-driven adapter capability gating.
    #
    # Rationale:
    #     Some providers expose multiple protocols/adapters (e.g., OpenAI chat_completions vs responses).
    #     Not every model supports every protocol, so routing must enforce an allowlist per model.
    supported_adapters: Optional[List[str]] = None

    # ADR-0064: Provider-native built-in tools capability gating.
    #
    # Rationale:
    #     Provider built-ins (e.g., OpenAI web_search) are protocol/model dependent.
    #     We declare which built-in tool names a model can support so callers can safely intersect
    #     configuration policy (allowlists) with model capability.
    supported_builtin_tools: Optional[List[str]] = None

    # ADR-0018: Model pricing configuration.
    #
    # Rationale:
    #     Pricing stored in ModelSpec (single source of truth) rather than hardcoded in
    #     CostCalculator. Allows operational updates and tenant-specific overrides via
    #     ModelProfile.pricing_overrides.
    pricing: Optional[ModelPricing] = None

    # Optional API base URL for this model (OpenAI-compatible adapters).
    #
    # Rationale:
    #     When set, the adapter uses this endpoint instead of the provider default.
    #     Enables the same model to be served by different hosts (e.g. official Moonshot
    #     vs proxy or self-hosted) via separate registry entries or overrides.
    base_url: Optional[str] = None

    # ADR-0116: Model provenance metadata (origin / vendor / license / open-weights).
    #
    # Rationale:
    #     Records intrinsic provenance facts as the single source of truth so a future
    #     deployment-level policy can gate model selection for provenance-restricted
    #     (e.g. defense/DARPA) environments. Storage only for now; the gate is a
    #     follow-on. None = unknown provenance.
    provenance: Optional[ModelProvenance] = None

    model_config = {"frozen": True}


# Global model registry
# Maps provider -> model_name -> ModelSpec
MODEL_REGISTRY: Dict[str, Dict[str, ModelSpec]] = {
    "mock": {
        "mock-small": ModelSpec(
            provider="mock",
            name="mock-small",
            display_name="Mock Small",
            # Mock can emit ThinkingEvent and generate_images for contract/UI tests.
            capabilities={
                CAP_STREAM,
                CAP_SYSTEM_PROMPT,
                CAP_REASONING,
                CAP_IMAGE_GENERATION,
            },
            max_output_tokens=512,
            supported_adapters=["mock"],
            default_adapter="mock",
            fallback_adapters=None,
            supported_builtin_tools=[],
            pricing=None,  # Free (mock)
        )
    },
    "openai": {
        # ADR-0113: image-generation models (image output, not chat). Routed to the
        # responses adapter, whose generate_images() targets the OpenAI Images API.
        "gpt-image-1": ModelSpec(
            provider="openai",
            name="gpt-image-1",
            display_name="GPT Image 1",
            capabilities={CAP_IMAGE_GENERATION},
            max_output_tokens=0,
            supported_adapters=["responses"],
            default_adapter="responses",
        ),
        "gpt-image-1.5": ModelSpec(
            provider="openai",
            name="gpt-image-1.5",
            display_name="GPT Image 1.5",
            capabilities={CAP_IMAGE_GENERATION},
            max_output_tokens=0,
            supported_adapters=["responses"],
            default_adapter="responses",
        ),
        "gpt-image-2": ModelSpec(
            provider="openai",
            name="gpt-image-2",
            display_name="GPT Image 2",
            capabilities={CAP_IMAGE_GENERATION},
            max_output_tokens=0,
            supported_adapters=["responses"],
            default_adapter="responses",
        ),
        "dall-e-3": ModelSpec(
            provider="openai",
            name="dall-e-3",
            display_name="DALL·E 3",
            capabilities={CAP_IMAGE_GENERATION},
            max_output_tokens=0,
            supported_adapters=["responses"],
            default_adapter="responses",
        ),
        "gpt-4o-mini": ModelSpec(
            provider="openai", 
            name="gpt-4o-mini",
            display_name="GPT-4o Mini",
            capabilities={
                CAP_STREAM, 
                CAP_SYSTEM_PROMPT, 
                CAP_TOOL_USE, 
                CAP_JSON_MODE, 
                CAP_VISION
            }, 
            max_output_tokens=16384,
            supported_adapters=["responses", "chat_completions"],
            default_adapter="responses",
            fallback_adapters=["chat_completions"],
            supported_builtin_tools=["openai.web_search"],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.00015"),
                output_per_1k=Decimal("0.0006"),
                cache_read_discount_pct=Decimal("50.0"),
            ),
        ),
        # Alias: same model as gpt-4o-mini but forced to chat_completions adapter (spec.name = provider model ID).
        "gpt-4o-mini-chat": ModelSpec(
            provider="openai",
            name="gpt-4o-mini",
            display_name="GPT-4o Mini (Chat Completions)",
            capabilities={
                CAP_STREAM,
                CAP_SYSTEM_PROMPT,
                CAP_TOOL_USE,
                CAP_JSON_MODE,
                CAP_VISION,
            },
            max_output_tokens=16384,
            supported_adapters=["chat_completions"],
            default_adapter="chat_completions",
            fallback_adapters=None,
            supported_builtin_tools=["openai.web_search"],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.00015"),
                output_per_1k=Decimal("0.0006"),
                cache_read_discount_pct=Decimal("50.0"),
            ),
        ),
        "gpt-4o": ModelSpec(
            provider="openai", 
            name="gpt-4o",
            display_name="GPT-4o",
            capabilities={
                CAP_STREAM, 
                CAP_SYSTEM_PROMPT, 
                CAP_TOOL_USE, 
                CAP_JSON_MODE, 
                CAP_VISION
            }, 
            max_output_tokens=16384,
            supported_adapters=["responses", "chat_completions"],
            default_adapter="responses",
            fallback_adapters=["chat_completions"],
            supported_builtin_tools=["openai.web_search"],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.0025"),
                output_per_1k=Decimal("0.01"),
                cache_read_discount_pct=Decimal("50.0"),
            ),
        ),
        "o3": ModelSpec(
            provider="openai", 
            name="o3",
            display_name="o3",
            capabilities={
                CAP_STREAM, 
                CAP_SYSTEM_PROMPT, 
                CAP_REASONING
            }, 
            max_output_tokens=65536,
            supported_adapters=["responses"],
            default_adapter="responses",
            fallback_adapters=None,
            supported_builtin_tools=[],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.01"),
                output_per_1k=Decimal("0.04"),
                cache_read_discount_pct=Decimal("50.0"),
                reasoning_per_1k=Decimal("0.04"),
            ),
        ),
        "o3-mini": ModelSpec(
            provider="openai", 
            name="o3-mini",
            display_name="o3 Mini",
            capabilities={
                CAP_STREAM, 
                CAP_SYSTEM_PROMPT, 
                CAP_REASONING
            }, 
            max_output_tokens=65536,
            supported_adapters=["responses"],
            default_adapter="responses",
            fallback_adapters=None,
            supported_builtin_tools=[],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.0011"),
                output_per_1k=Decimal("0.0044"),
                cache_read_discount_pct=Decimal("50.0"),
                reasoning_per_1k=Decimal("0.0044"),
            ),
        ),
        # GPT-5.6 family — current OpenAI generation (GA July 9, 2026): Sol (flagship),
        # Terra (balanced), Luna (cost-efficient). All tiers: 1.05M context, 128k max
        # output, multimodal + reasoning-effort. Alias "gpt-5.6" routes to Sol.
        # Pricing per 1M: Sol $5/$30, Terra $2.50/$15, Luna $1/$6; cache reads -90%.
        "gpt-5.6-sol": ModelSpec(
            provider="openai",
            name="gpt-5.6-sol",
            display_name="GPT-5.6 Sol",
            capabilities={
                CAP_STREAM,
                CAP_SYSTEM_PROMPT,
                CAP_TOOL_USE,
                CAP_JSON_MODE,
                CAP_VISION,
                CAP_REASONING,
            },
            max_output_tokens=128000,
            released_at=date(2026, 7, 9),
            supported_adapters=["responses", "chat_completions"],
            default_adapter="responses",
            fallback_adapters=["chat_completions"],
            supported_builtin_tools=["openai.web_search"],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.005"),
                output_per_1k=Decimal("0.03"),
                cache_read_discount_pct=Decimal("90.0"),
                reasoning_per_1k=Decimal("0.03"),
            ),
        ),
        # Alias: OpenAI routes "gpt-5.6" to the Sol tier.
        "gpt-5.6": ModelSpec(
            provider="openai",
            name="gpt-5.6-sol",
            display_name="GPT-5.6 (Sol)",
            capabilities={
                CAP_STREAM,
                CAP_SYSTEM_PROMPT,
                CAP_TOOL_USE,
                CAP_JSON_MODE,
                CAP_VISION,
                CAP_REASONING,
            },
            max_output_tokens=128000,
            released_at=date(2026, 7, 9),
            supported_adapters=["responses", "chat_completions"],
            default_adapter="responses",
            fallback_adapters=["chat_completions"],
            supported_builtin_tools=["openai.web_search"],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.005"),
                output_per_1k=Decimal("0.03"),
                cache_read_discount_pct=Decimal("90.0"),
                reasoning_per_1k=Decimal("0.03"),
            ),
        ),
        "gpt-5.6-terra": ModelSpec(
            provider="openai",
            name="gpt-5.6-terra",
            display_name="GPT-5.6 Terra",
            capabilities={
                CAP_STREAM,
                CAP_SYSTEM_PROMPT,
                CAP_TOOL_USE,
                CAP_JSON_MODE,
                CAP_VISION,
                CAP_REASONING,
            },
            max_output_tokens=128000,
            released_at=date(2026, 7, 9),
            supported_adapters=["responses", "chat_completions"],
            default_adapter="responses",
            fallback_adapters=["chat_completions"],
            supported_builtin_tools=["openai.web_search"],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.0025"),
                output_per_1k=Decimal("0.015"),
                cache_read_discount_pct=Decimal("90.0"),
                reasoning_per_1k=Decimal("0.015"),
            ),
        ),
        "gpt-5.6-luna": ModelSpec(
            provider="openai",
            name="gpt-5.6-luna",
            display_name="GPT-5.6 Luna",
            capabilities={
                CAP_STREAM,
                CAP_SYSTEM_PROMPT,
                CAP_TOOL_USE,
                CAP_JSON_MODE,
                CAP_VISION,
                CAP_REASONING,
            },
            max_output_tokens=128000,
            released_at=date(2026, 7, 9),
            supported_adapters=["responses", "chat_completions"],
            default_adapter="responses",
            fallback_adapters=["chat_completions"],
            supported_builtin_tools=["openai.web_search"],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.001"),
                output_per_1k=Decimal("0.006"),
                cache_read_discount_pct=Decimal("90.0"),
                reasoning_per_1k=Decimal("0.006"),
            ),
        ),
        # GPT-5.5 family — previous OpenAI flagship (released April 2026).
        # Uses existing openai responses/chat_completions adapters unchanged.
        # Pricing: $5/$30 per 1M tokens; cached input is $0.50 per 1M tokens.
        "gpt-5.5": ModelSpec(
            provider="openai",
            name="gpt-5.5",
            display_name="GPT-5.5",
            capabilities={
                CAP_STREAM,
                CAP_SYSTEM_PROMPT,
                CAP_TOOL_USE,
                CAP_JSON_MODE,
                CAP_VISION,
                CAP_REASONING,
            },
            max_output_tokens=128000,
            supported_adapters=["responses", "chat_completions"],
            default_adapter="responses",
            fallback_adapters=["chat_completions"],
            supported_builtin_tools=["openai.web_search"],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.005"),
                output_per_1k=Decimal("0.03"),
                cache_read_discount_pct=Decimal("90.0"),
                reasoning_per_1k=Decimal("0.03"),
            ),
        ),
        # GPT-5.4 family — previous OpenAI flagship (released March 5, 2026).
        # Uses existing openai responses/chat_completions adapters unchanged.
        # Pricing: $2.50/$10 per 1M tokens (flagship), $0.75/$3 (mini), $0.20/$0.80 (nano).
        "gpt-5.4": ModelSpec(
            provider="openai",
            name="gpt-5.4",
            display_name="GPT-5.4",
            capabilities={
                CAP_STREAM,
                CAP_SYSTEM_PROMPT,
                CAP_TOOL_USE,
                CAP_JSON_MODE,
                CAP_VISION,
                CAP_REASONING,
            },
            max_output_tokens=32768,
            supported_adapters=["responses", "chat_completions"],
            default_adapter="responses",
            fallback_adapters=["chat_completions"],
            supported_builtin_tools=["openai.web_search"],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.0025"),
                output_per_1k=Decimal("0.01"),
                cache_read_discount_pct=Decimal("50.0"),
                reasoning_per_1k=Decimal("0.01"),
            ),
        ),
        "gpt-5.4-mini": ModelSpec(
            provider="openai",
            name="gpt-5.4-mini",
            display_name="GPT-5.4 Mini",
            capabilities={
                CAP_STREAM,
                CAP_SYSTEM_PROMPT,
                CAP_TOOL_USE,
                CAP_JSON_MODE,
                CAP_VISION,
            },
            max_output_tokens=16384,
            supported_adapters=["responses", "chat_completions"],
            default_adapter="responses",
            fallback_adapters=["chat_completions"],
            supported_builtin_tools=["openai.web_search"],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.00075"),
                output_per_1k=Decimal("0.003"),
                cache_read_discount_pct=Decimal("50.0"),
            ),
        ),
        "gpt-5.4-nano": ModelSpec(
            provider="openai",
            name="gpt-5.4-nano",
            display_name="GPT-5.4 Nano",
            capabilities={
                CAP_STREAM,
                CAP_SYSTEM_PROMPT,
                CAP_TOOL_USE,
                CAP_JSON_MODE,
            },
            max_output_tokens=8192,
            supported_adapters=["responses", "chat_completions"],
            default_adapter="responses",
            fallback_adapters=["chat_completions"],
            supported_builtin_tools=[],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.0002"),
                output_per_1k=Decimal("0.0008"),
                cache_read_discount_pct=Decimal("50.0"),
            ),
        ),
        "gpt-5.2": ModelSpec(
            provider="openai", 
            name="gpt-5.2",
            display_name="GPT-5.2",
            capabilities={
                CAP_STREAM, 
                CAP_SYSTEM_PROMPT, 
                CAP_TOOL_USE, 
                CAP_JSON_MODE, 
                CAP_VISION, 
                CAP_REASONING
            }, 
            max_output_tokens=16384,
            supported_adapters=["responses", "chat_completions"],
            default_adapter="responses",
            fallback_adapters=["chat_completions"],
            supported_builtin_tools=["openai.web_search"],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.005"),
                output_per_1k=Decimal("0.02"),
                cache_read_discount_pct=Decimal("50.0"),
                reasoning_per_1k=Decimal("0.02"),
            ),
        ),
        "gpt-5.1": ModelSpec(
            provider="openai", 
            name="gpt-5.1",
            display_name="GPT-5.1",
            capabilities={
                CAP_STREAM, 
                CAP_SYSTEM_PROMPT, 
                CAP_TOOL_USE, 
                CAP_JSON_MODE, 
                CAP_VISION, 
                CAP_REASONING
            }, 
            max_output_tokens=16384,
            supported_adapters=["responses", "chat_completions"],
            default_adapter="responses",
            fallback_adapters=["chat_completions"],
            supported_builtin_tools=["openai.web_search"],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.004"),
                output_per_1k=Decimal("0.016"),
                cache_read_discount_pct=Decimal("50.0"),
                reasoning_per_1k=Decimal("0.016"),
            ),
        ),
        "gpt-5": ModelSpec(
            provider="openai", 
            name="gpt-5",
            display_name="GPT-5",
            capabilities={
                CAP_STREAM, 
                CAP_SYSTEM_PROMPT, 
                CAP_TOOL_USE, 
                CAP_JSON_MODE, 
                CAP_VISION, 
                CAP_REASONING
            }, 
            max_output_tokens=16384,
            supported_adapters=["responses", "chat_completions"],
            default_adapter="responses",
            fallback_adapters=["chat_completions"],
            supported_builtin_tools=["openai.web_search"],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.003"),
                output_per_1k=Decimal("0.012"),
                cache_read_discount_pct=Decimal("50.0"),
                reasoning_per_1k=Decimal("0.012"),
            ),
        ),
        "gpt-5-mini": ModelSpec(
            provider="openai", 
            name="gpt-5-mini",
            display_name="GPT-5 Mini",
            capabilities={
                CAP_STREAM, 
                CAP_SYSTEM_PROMPT, 
                CAP_TOOL_USE, 
                CAP_JSON_MODE, 
                CAP_VISION
            }, 
            max_output_tokens=16384,
            supported_adapters=["responses", "chat_completions"],
            default_adapter="responses",
            fallback_adapters=["chat_completions"],
            supported_builtin_tools=["openai.web_search"],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.0002"),
                output_per_1k=Decimal("0.0008"),
                cache_read_discount_pct=Decimal("50.0"),
            ),
        ),
        "gpt-5-nano": ModelSpec(
            provider="openai", 
            name="gpt-5-nano",
            display_name="GPT-5 Nano",
            capabilities={
                CAP_STREAM, 
                CAP_SYSTEM_PROMPT, 
                CAP_TOOL_USE, 
                CAP_JSON_MODE
            }, 
            max_output_tokens=8192,
            supported_adapters=["responses", "chat_completions"],
            default_adapter="responses",
            fallback_adapters=["chat_completions"],
            supported_builtin_tools=["openai.web_search"],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.0001"),
                output_per_1k=Decimal("0.0004"),
                cache_read_discount_pct=Decimal("50.0"),
            ),
        ),
        "gpt-4.1": ModelSpec(
            provider="openai", 
            name="gpt-4.1",
            display_name="GPT-4.1",
            capabilities={
                CAP_STREAM, 
                CAP_SYSTEM_PROMPT, 
                CAP_TOOL_USE, 
                CAP_JSON_MODE, 
                CAP_VISION
            }, 
            max_output_tokens=16384,
            supported_adapters=["responses", "chat_completions"],
            default_adapter="responses",
            fallback_adapters=["chat_completions"],
            supported_builtin_tools=["openai.web_search"],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.002"),
                output_per_1k=Decimal("0.008"),
                cache_read_discount_pct=Decimal("50.0"),
            ),
        ),
        "gpt-4.1-mini": ModelSpec(
            provider="openai", 
            name="gpt-4.1-mini",
            display_name="GPT-4.1 Mini",
            capabilities={
                CAP_STREAM, 
                CAP_SYSTEM_PROMPT, 
                CAP_TOOL_USE, 
                CAP_JSON_MODE, 
                CAP_VISION
            }, 
            max_output_tokens=16384,
            supported_adapters=["responses", "chat_completions"],
            default_adapter="responses",
            fallback_adapters=["chat_completions"],
            supported_builtin_tools=["openai.web_search"],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.0004"),
                output_per_1k=Decimal("0.0016"),
                cache_read_discount_pct=Decimal("50.0"),
            ),
        ),
        # Alias: same model as gpt-4.1-mini but forced to chat_completions adapter (spec.name = provider model ID).
        "gpt-4.1-mini-chat": ModelSpec(
            provider="openai",
            name="gpt-4.1-mini",
            display_name="GPT-4.1 Mini (Chat Completions)",
            capabilities={
                CAP_STREAM,
                CAP_SYSTEM_PROMPT,
                CAP_TOOL_USE,
                CAP_JSON_MODE,
                CAP_VISION,
            },
            max_output_tokens=16384,
            supported_adapters=["chat_completions"],
            default_adapter="chat_completions",
            fallback_adapters=None,
            supported_builtin_tools=["openai.web_search"],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.0004"),
                output_per_1k=Decimal("0.0016"),
                cache_read_discount_pct=Decimal("50.0"),
            ),
        ),
        "gpt-4.1-nano": ModelSpec(
            provider="openai",
            name="gpt-4.1-nano",
            display_name="GPT-4.1 Nano",
            capabilities={
                CAP_STREAM,
                CAP_SYSTEM_PROMPT,
                CAP_TOOL_USE,
                CAP_JSON_MODE,
            },
            max_output_tokens=8192,
            supported_adapters=["responses", "chat_completions"],
            default_adapter="responses",
            fallback_adapters=["chat_completions"],
            # No Motet-wired provider built-ins; core.web_search uses DuckDuckGo.
            supported_builtin_tools=[],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.0001"),
                output_per_1k=Decimal("0.0004"),
                cache_read_discount_pct=Decimal("50.0"),
            ),
        ),
    },
    "anthropic": {
        # NOTE: Anthropic model availability is account-dependent. The canonical model IDs below
        # are those returned by Anthropic's Models API (`GET /v1/models`) for at least one
        # real key we validated in Docker integration tests.
        #
        # Current models (July 2026) — source: platform.claude.com/docs/about-claude/models
        # and GET /v1/models (validated against a live key on 2026-07-25):
        #   claude-fable-5    — next-gen top end for long-horizon agents (adaptive thinking always on)
        #   claude-opus-5     — current Opus (alias ID, no date suffix required); thinking on by default
        #   claude-sonnet-5   — current balanced (alias ID, no date suffix required)
        #   claude-haiku-4-5  — current fastest (versioned: claude-haiku-4-5-20251001)
        # claude-mythos-5 exists but is restricted (Project Glasswing) — intentionally unlisted.
        # Extended output: Opus 4.7+/Sonnet 4.6+ support up to 300k output tokens
        #   via the `output-300k-2026-03-24` beta header on the Message Batches API.
        # Deprecated: claude-sonnet-4-20250514, claude-opus-4-20250514

        # ── Current generation (5-series) ────────────────────────────────────────
        # Fable 5 (Jun 7, 2026): 1M context, 128k output, adaptive thinking always on.
        # Pricing: $10/M input, $50/M output.
        "claude-fable-5": ModelSpec(
            provider="anthropic",
            name="claude-fable-5",
            display_name="Claude Fable 5",
            capabilities={
                CAP_STREAM,
                CAP_SYSTEM_PROMPT,
                CAP_TOOL_USE,
                CAP_JSON_MODE,
                CAP_REASONING,
                CAP_VISION,
            },
            max_output_tokens=128000,
            released_at=date(2026, 6, 7),
            supported_adapters=["messages"],
            default_adapter="messages",
            fallback_adapters=None,
            supported_builtin_tools=["anthropic.web_search"],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.01"),
                output_per_1k=Decimal("0.05"),
                cache_read_discount_pct=Decimal("90.0"),
                reasoning_per_1k=Decimal("0.05"),
            ),
        ),
        # Opus 5 (Jul 24, 2026): 1M context (default=max), 128k output, adaptive thinking
        # on by default. Effort ladder: low/medium/high/xhigh/max (API default high).
        # Disabling thinking requires effort high or below (xhigh/max + disabled → 400).
        # Pricing: $5/M input, $25/M output (same as Opus 4.8).
        "claude-opus-5": ModelSpec(
            provider="anthropic",
            name="claude-opus-5",
            display_name="Claude Opus 5",
            capabilities={
                CAP_STREAM,
                CAP_SYSTEM_PROMPT,
                CAP_TOOL_USE,
                CAP_JSON_MODE,
                CAP_REASONING,
                CAP_VISION,
            },
            max_output_tokens=128000,
            released_at=date(2026, 7, 24),
            supported_adapters=["messages"],
            default_adapter="messages",
            fallback_adapters=None,
            supported_builtin_tools=["anthropic.web_search"],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.005"),
                output_per_1k=Decimal("0.025"),
                cache_read_discount_pct=Decimal("90.0"),
                reasoning_per_1k=Decimal("0.025"),
            ),
        ),
        # Opus 4.8 (May 28, 2026): previous Opus; 1M context, 128k output, adaptive thinking.
        # Pricing: $5/M input, $25/M output. Prefer claude-opus-5 for new work.
        "claude-opus-4-8": ModelSpec(
            provider="anthropic",
            name="claude-opus-4-8",
            display_name="Claude Opus 4.8",
            capabilities={
                CAP_STREAM,
                CAP_SYSTEM_PROMPT,
                CAP_TOOL_USE,
                CAP_JSON_MODE,
                CAP_REASONING,
                CAP_VISION,
            },
            max_output_tokens=128000,
            released_at=date(2026, 5, 28),
            supported_adapters=["messages"],
            default_adapter="messages",
            fallback_adapters=None,
            supported_builtin_tools=["anthropic.web_search"],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.005"),
                output_per_1k=Decimal("0.025"),
                cache_read_discount_pct=Decimal("90.0"),
                reasoning_per_1k=Decimal("0.025"),
            ),
        ),
        # Convenience alias matching dot-notation convention
        "claude-opus-4.8": ModelSpec(
            provider="anthropic",
            name="claude-opus-4-8",
            display_name="Claude Opus 4.8",
            capabilities={
                CAP_STREAM,
                CAP_SYSTEM_PROMPT,
                CAP_TOOL_USE,
                CAP_JSON_MODE,
                CAP_REASONING,
                CAP_VISION,
            },
            max_output_tokens=128000,
            released_at=date(2026, 5, 28),
            supported_adapters=["messages"],
            default_adapter="messages",
            fallback_adapters=None,
            supported_builtin_tools=["anthropic.web_search"],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.005"),
                output_per_1k=Decimal("0.025"),
                cache_read_discount_pct=Decimal("90.0"),
                reasoning_per_1k=Decimal("0.025"),
            ),
        ),
        # Sonnet 5 (Jun 29, 2026): 1M context, 128k output, adaptive thinking.
        # Pricing: $3/M input, $15/M output (intro $2/$10 through 2026-08-31 not encoded).
        "claude-sonnet-5": ModelSpec(
            provider="anthropic",
            name="claude-sonnet-5",
            display_name="Claude Sonnet 5",
            capabilities={
                CAP_STREAM,
                CAP_SYSTEM_PROMPT,
                CAP_TOOL_USE,
                CAP_JSON_MODE,
                CAP_REASONING,
                CAP_VISION,
            },
            max_output_tokens=128000,
            released_at=date(2026, 6, 29),
            supported_adapters=["messages"],
            default_adapter="messages",
            fallback_adapters=None,
            supported_builtin_tools=["anthropic.web_search"],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.003"),
                output_per_1k=Decimal("0.015"),
                cache_read_discount_pct=Decimal("90.0"),
                reasoning_per_1k=Decimal("0.015"),
            ),
        ),

        # ── Previous flagship (consider migrating to Opus 5) ─────────────────────
        # Released April 16, 2026. Major vision upgrade: 98.5% visual acuity (up from 54.5%),
        # 3x image resolution (3.75MP / 2,576px long edge), -21% document reasoning errors.
        # DocVQA 94.8%, ChartQA 86.9%, MMMU 72.1%. Max output 128k tokens (sync Messages API);
        # up to 300k via output-300k-2026-03-24 beta header on Message Batches API.
        # Pricing: $5/M input, $25/M output.
        "claude-opus-4-7": ModelSpec(
            provider="anthropic",
            name="claude-opus-4-7",
            display_name="Claude Opus 4.7",
            capabilities={
                CAP_STREAM,
                CAP_SYSTEM_PROMPT,
                CAP_TOOL_USE,
                CAP_JSON_MODE,
                CAP_REASONING,
                CAP_VISION,
            },
            max_output_tokens=128000,
            supported_adapters=["messages"],
            default_adapter="messages",
            fallback_adapters=None,
            supported_builtin_tools=["anthropic.web_search"],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.005"),
                output_per_1k=Decimal("0.025"),
                cache_read_discount_pct=Decimal("90.0"),
                reasoning_per_1k=Decimal("0.025"),
            ),
        ),
        # Convenience alias matching dot-notation convention
        "claude-opus-4.7": ModelSpec(
            provider="anthropic",
            name="claude-opus-4-7",
            display_name="Claude Opus 4.7",
            capabilities={
                CAP_STREAM,
                CAP_SYSTEM_PROMPT,
                CAP_TOOL_USE,
                CAP_JSON_MODE,
                CAP_REASONING,
                CAP_VISION,
            },
            max_output_tokens=128000,
            supported_adapters=["messages"],
            default_adapter="messages",
            fallback_adapters=None,
            supported_builtin_tools=["anthropic.web_search"],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.005"),
                output_per_1k=Decimal("0.025"),
                cache_read_discount_pct=Decimal("90.0"),
                reasoning_per_1k=Decimal("0.025"),
            ),
        ),

        # ── Current balanced ─────────────────────────────────────────────────────
        # Released Feb 17, 2026. Approaches Opus 4.6 on OfficeQA (enterprise document reading).
        # MMMU-Pro 74.5-75.6%, OSWorld 72.5%. 1M context, 64k max output.
        # Pricing: $3/M input, $15/M output.
        "claude-sonnet-4-6": ModelSpec(
            provider="anthropic",
            name="claude-sonnet-4-6",
            display_name="Claude Sonnet 4.6",
            capabilities={
                CAP_STREAM,
                CAP_SYSTEM_PROMPT,
                CAP_TOOL_USE,
                CAP_JSON_MODE,
                CAP_REASONING,
                CAP_VISION,
            },
            max_output_tokens=64000,
            supported_adapters=["messages"],
            default_adapter="messages",
            fallback_adapters=None,
            supported_builtin_tools=["anthropic.web_search"],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.003"),
                output_per_1k=Decimal("0.015"),
                cache_read_discount_pct=Decimal("90.0"),
                reasoning_per_1k=Decimal("0.015"),
            ),
        ),
        # Convenience alias
        "claude-sonnet-4.6": ModelSpec(
            provider="anthropic",
            name="claude-sonnet-4-6",
            display_name="Claude Sonnet 4.6",
            capabilities={
                CAP_STREAM,
                CAP_SYSTEM_PROMPT,
                CAP_TOOL_USE,
                CAP_JSON_MODE,
                CAP_REASONING,
                CAP_VISION,
            },
            max_output_tokens=64000,
            supported_adapters=["messages"],
            default_adapter="messages",
            fallback_adapters=None,
            supported_builtin_tools=["anthropic.web_search"],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.003"),
                output_per_1k=Decimal("0.015"),
                cache_read_discount_pct=Decimal("90.0"),
                reasoning_per_1k=Decimal("0.015"),
            ),
        ),

        # ── Previous flagship, still available (consider migrating to 4.7) ───────
        "claude-opus-4-6": ModelSpec(
            provider="anthropic",
            name="claude-opus-4-6",
            display_name="Claude Opus 4.6",
            capabilities={
                CAP_STREAM,
                CAP_SYSTEM_PROMPT,
                CAP_TOOL_USE,
                CAP_JSON_MODE,
                CAP_REASONING,
                CAP_VISION,
            },
            max_output_tokens=16000,
            supported_adapters=["messages"],
            default_adapter="messages",
            fallback_adapters=None,
            supported_builtin_tools=["anthropic.web_search"],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.015"),
                output_per_1k=Decimal("0.075"),
                cache_read_discount_pct=Decimal("90.0"),
                reasoning_per_1k=Decimal("0.075"),
            ),
        ),

        # ── Deprecated models (kept for backwards compatibility) ─────────────────
        # claude-sonnet-4-20250514 and claude-opus-4-20250514 are deprecated by Anthropic.
        # Migrate to claude-sonnet-4-6 / claude-opus-4-7. These will be removed in a future release.
        # Deprecated — migrate to claude-sonnet-4-6
        "claude-sonnet-4-20250514": ModelSpec(
            provider="anthropic",
            name="claude-sonnet-4-20250514",
            display_name="Claude Sonnet 4 (deprecated)",
            capabilities={
                CAP_STREAM,
                CAP_SYSTEM_PROMPT,
                CAP_TOOL_USE,
                CAP_JSON_MODE,
                CAP_REASONING,
                CAP_VISION,
            },
            max_output_tokens=8192,
            supported_adapters=["messages"],
            default_adapter="messages",
            fallback_adapters=None,
            supported_builtin_tools=["anthropic.web_search"],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.003"),
                output_per_1k=Decimal("0.015"),
                cache_read_discount_pct=Decimal("90.0"),
                reasoning_per_1k=Decimal("0.015"),
            ),
        ),
        # Deprecated — migrate to claude-opus-4-7
        "claude-opus-4-20250514": ModelSpec(
            provider="anthropic",
            name="claude-opus-4-20250514",
            display_name="Claude Opus 4 (deprecated)",
            capabilities={
                CAP_STREAM,
                CAP_SYSTEM_PROMPT,
                CAP_TOOL_USE,
                CAP_JSON_MODE,
                CAP_REASONING,
                CAP_VISION,
            },
            max_output_tokens=8192,
            supported_adapters=["messages"],
            default_adapter="messages",
            fallback_adapters=None,
            supported_builtin_tools=["anthropic.web_search"],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.015"),
                output_per_1k=Decimal("0.075"),
                cache_read_discount_pct=Decimal("90.0"),
                reasoning_per_1k=Decimal("0.075"),
            ),
        ),
        "claude-opus-4-1-20250805": ModelSpec(
            provider="anthropic",
            name="claude-opus-4-1-20250805",
            display_name="Claude Opus 4.1",
            capabilities={
                CAP_STREAM,
                CAP_SYSTEM_PROMPT,
                CAP_TOOL_USE,
                CAP_JSON_MODE,
                CAP_REASONING,
                CAP_VISION,
            },
            max_output_tokens=8192,
            supported_adapters=["messages"],
            default_adapter="messages",
            fallback_adapters=None,
            supported_builtin_tools=["anthropic.web_search"],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.015"),
                output_per_1k=Decimal("0.075"),
                cache_read_discount_pct=Decimal("90.0"),
                reasoning_per_1k=Decimal("0.075"),
            ),
        ),
        "claude-sonnet-4-5-20250929": ModelSpec(
            provider="anthropic",
            name="claude-sonnet-4-5-20250929",
            display_name="Claude Sonnet 4.5",
            capabilities={
                CAP_STREAM,
                CAP_SYSTEM_PROMPT,
                CAP_TOOL_USE,
                CAP_JSON_MODE,
                CAP_REASONING,
                CAP_VISION,
            },
            max_output_tokens=8192,
            supported_adapters=["messages"],
            default_adapter="messages",
            fallback_adapters=None,
            supported_builtin_tools=["anthropic.web_search"],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.003"),
                output_per_1k=Decimal("0.015"),
                cache_read_discount_pct=Decimal("90.0"),
                reasoning_per_1k=Decimal("0.015"),
            ),
        ),
        "claude-haiku-4-5-20251001": ModelSpec(
            provider="anthropic",
            name="claude-haiku-4-5-20251001",
            display_name="Claude Haiku 4.5",
            capabilities={
                CAP_STREAM,
                CAP_SYSTEM_PROMPT,
                CAP_TOOL_USE,
                CAP_JSON_MODE,
                CAP_VISION,
            },
            max_output_tokens=8192,
            supported_adapters=["messages"],
            default_adapter="messages",
            fallback_adapters=None,
            supported_builtin_tools=["anthropic.web_search"],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.0008"),
                output_per_1k=Decimal("0.004"),
                cache_read_discount_pct=Decimal("90.0"),
            ),
        ),
        "claude-opus-4-5-20251101": ModelSpec(
            provider="anthropic",
            name="claude-opus-4-5-20251101",
            display_name="Claude Opus 4.5",
            capabilities={
                CAP_STREAM,
                CAP_SYSTEM_PROMPT,
                CAP_TOOL_USE,
                CAP_JSON_MODE,
                CAP_REASONING,
                CAP_VISION,
            },
            max_output_tokens=8192,
            supported_adapters=["messages"],
            default_adapter="messages",
            fallback_adapters=None,
            supported_builtin_tools=["anthropic.web_search"],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.015"),
                output_per_1k=Decimal("0.075"),
                cache_read_discount_pct=Decimal("90.0"),
                reasoning_per_1k=Decimal("0.075"),
            ),
        ),
        # Convenience alias: "claude-sonnet-4.5" → resolves to versioned API model name
        "claude-sonnet-4.5": ModelSpec(
            provider="anthropic", 
            name="claude-sonnet-4-5-20250929",
            display_name="Claude Sonnet 4.5",
            capabilities={
                CAP_STREAM, 
                CAP_SYSTEM_PROMPT, 
                CAP_TOOL_USE, 
                CAP_JSON_MODE, 
                CAP_REASONING,
                CAP_VISION,
            }, 
            max_output_tokens=8192,
            supported_adapters=["messages"],
            default_adapter="messages",
            fallback_adapters=None,
            supported_builtin_tools=["anthropic.web_search"],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.003"),
                output_per_1k=Decimal("0.015"),
                cache_read_discount_pct=Decimal("90.0"),
                reasoning_per_1k=Decimal("0.015"),
            ),
        ),
    },
    "moonshot": {
        # Moonshot AI (Kimi) - Uses dedicated MoonshotChatCompletionsAdapter
        # Handles reasoning_content replay, $web_search builtin, temperature constraints
        # Credentials: vault key "moonshot" or config moonshot_api_key
        # See https://platform.moonshot.ai/docs/pricing/chat for model list and pricing.
        "kimi-k2.5": ModelSpec(
            provider="moonshot",
            name="kimi-k2.5",
            display_name="Kimi K2.5",
            capabilities={
                CAP_STREAM,
                CAP_SYSTEM_PROMPT,
                CAP_TOOL_USE,
                CAP_JSON_MODE,
                CAP_VISION,
                CAP_REASONING,
            },
            max_output_tokens=65536,
            supported_adapters=["chat_completions"],
            default_adapter="chat_completions",
            fallback_adapters=None,
            supported_builtin_tools=["moonshot.web_search"],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.0006"),
                output_per_1k=Decimal("0.003"),
                cache_read_discount_pct=Decimal("83.33"),
            ),
            base_url="https://api.moonshot.ai/v1",
        ),
        # K2 generation model: code/agent, 256k context. No vision.
        "kimi-k2-0905-preview": ModelSpec(
            provider="moonshot",
            name="kimi-k2-0905-preview",
            display_name="Kimi K2 (0905)",
            capabilities={
                CAP_STREAM,
                CAP_SYSTEM_PROMPT,
                CAP_TOOL_USE,
                CAP_JSON_MODE,
                CAP_REASONING,
            },
            max_output_tokens=65536,
            supported_adapters=["chat_completions"],
            default_adapter="chat_completions",
            fallback_adapters=None,
            supported_builtin_tools=["moonshot.web_search"],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.0006"),
                output_per_1k=Decimal("0.0025"),
                cache_read_discount_pct=Decimal("75.0"),
            ),
            base_url="https://api.moonshot.ai/v1",
        ),
        # K2 thinking model: deep reasoning, 256k context. Uses reasoning_content replay like K2.5. No vision.
        "kimi-k2-thinking": ModelSpec(
            provider="moonshot",
            name="kimi-k2-thinking",
            display_name="Kimi K2 Thinking",
            capabilities={
                CAP_STREAM,
                CAP_SYSTEM_PROMPT,
                CAP_TOOL_USE,
                CAP_JSON_MODE,
                CAP_REASONING,
            },
            max_output_tokens=65536,
            supported_adapters=["chat_completions"],
            default_adapter="chat_completions",
            fallback_adapters=None,
            supported_builtin_tools=["moonshot.web_search"],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.0006"),
                output_per_1k=Decimal("0.0025"),
                cache_read_discount_pct=Decimal("75.0"),
            ),
            base_url="https://api.moonshot.ai/v1",
        ),
        # Coding-specialized K2.7 (ADR-0122 cheap challenger). No vision unless verified.
        "kimi-k2.7-code": ModelSpec(
            provider="moonshot",
            name="kimi-k2.7-code",
            display_name="Kimi K2.7 Code",
            capabilities={
                CAP_STREAM,
                CAP_SYSTEM_PROMPT,
                CAP_TOOL_USE,
                CAP_JSON_MODE,
                CAP_REASONING,
            },
            max_output_tokens=65536,
            supported_adapters=["chat_completions"],
            default_adapter="chat_completions",
            fallback_adapters=None,
            supported_builtin_tools=["moonshot.web_search"],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.00095"),
                output_per_1k=Decimal("0.004"),
                cache_read_discount_pct=Decimal("80.0"),
            ),
            base_url="https://api.moonshot.ai/v1",
        ),
        # Kimi K3 flagship (Jul 2026): 1M context, native vision, always-on thinking via
        # top-level reasoning_effort (not K2.x thinking{}). Chat Completions only.
        # Builtin web_search omitted while Moonshot Formula search is being updated.
        "kimi-k3": ModelSpec(
            provider="moonshot",
            name="kimi-k3",
            display_name="Kimi K3",
            capabilities={
                CAP_STREAM,
                CAP_SYSTEM_PROMPT,
                CAP_TOOL_USE,
                CAP_JSON_MODE,
                CAP_VISION,
                CAP_REASONING,
            },
            max_output_tokens=131072,
            supported_adapters=["chat_completions"],
            default_adapter="chat_completions",
            fallback_adapters=None,
            # Formula web_search being updated; use core.web_search / MCP until stable.
            supported_builtin_tools=[],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.003"),
                output_per_1k=Decimal("0.015"),
                cache_read_discount_pct=Decimal("90.0"),
            ),
            base_url="https://api.moonshot.ai/v1",
        ),
    },
    "xai": {
        # SpaceXAI / xAI Grok via OpenAI-compatible Responses API (ADR-0122).
        # Credentials: vault key "xai" or config xai_api_key (MOTET_XAI_API_KEY / XAI_API_KEY).
        "grok-4.5": ModelSpec(
            provider="xai",
            name="grok-4.5",
            display_name="Grok 4.5",
            capabilities={
                CAP_STREAM,
                CAP_SYSTEM_PROMPT,
                CAP_TOOL_USE,
                CAP_JSON_MODE,
                CAP_VISION,
                CAP_REASONING,
            },
            max_output_tokens=131072,
            supported_adapters=["responses"],
            default_adapter="responses",
            fallback_adapters=None,
            supported_builtin_tools=["xai.web_search"],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.002"),
                output_per_1k=Decimal("0.006"),
                cache_read_discount_pct=Decimal("75.0"),
            ),
            base_url="https://api.x.ai/v1",
        ),
        # Grok 4.6 (2026-08-12): 500k context, text+image in / text out, always-on
        # reasoning (low|medium|high|xhigh; API default high). Same Responses path
        # and Motet reasoning policy as 4.5 (adapter forces medium unless set).
        # Pricing: $2 / $0.50 cached / $6 per 1M tokens below 200k prompt tokens.
        "grok-4.6": ModelSpec(
            provider="xai",
            name="grok-4.6",
            display_name="Grok 4.6",
            capabilities={
                CAP_STREAM,
                CAP_SYSTEM_PROMPT,
                CAP_TOOL_USE,
                CAP_JSON_MODE,
                CAP_VISION,
                CAP_REASONING,
            },
            max_output_tokens=131072,
            supported_adapters=["responses"],
            default_adapter="responses",
            fallback_adapters=None,
            supported_builtin_tools=["xai.web_search"],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.002"),
                output_per_1k=Decimal("0.006"),
                cache_read_discount_pct=Decimal("75.0"),
            ),
            base_url="https://api.x.ai/v1",
            released_at=date(2026, 8, 12),
        ),
    },
    "meta": {
        # Meta Model API — Muse Spark via OpenAI-compatible Responses.
        # Credentials: vault key "meta" or config meta_api_key
        # (MOTET_META_API_KEY / MODEL_API_KEY / META_API_KEY).
        # See https://ai.developer.meta.com/docs/models
        # Standard-tier pricing only; contributor (train-on-your-data) is not registered.
        "muse-spark-1.1": ModelSpec(
            provider="meta",
            name="muse-spark-1.1",
            display_name="Muse Spark 1.1",
            capabilities={
                CAP_STREAM,
                CAP_SYSTEM_PROMPT,
                CAP_TOOL_USE,
                CAP_JSON_MODE,
                CAP_VISION,
                CAP_REASONING,
            },
            max_output_tokens=131072,
            supported_adapters=["responses"],
            default_adapter="responses",
            fallback_adapters=None,
            supported_builtin_tools=["meta.web_search"],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.00125"),
                output_per_1k=Decimal("0.00425"),
                cache_read_discount_pct=Decimal("88.0"),
            ),
            base_url="https://api.meta.ai/v1",
            released_at=date(2026, 7, 1),
        ),
        "muse-spark-1.2": ModelSpec(
            provider="meta",
            name="muse-spark-1.2",
            display_name="Muse Spark 1.2",
            capabilities={
                CAP_STREAM,
                CAP_SYSTEM_PROMPT,
                CAP_TOOL_USE,
                CAP_JSON_MODE,
                CAP_VISION,
                CAP_REASONING,
            },
            max_output_tokens=131072,
            supported_adapters=["responses"],
            default_adapter="responses",
            fallback_adapters=None,
            supported_builtin_tools=["meta.web_search"],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.00125"),
                output_per_1k=Decimal("0.00425"),
                cache_read_discount_pct=Decimal("88.0"),
            ),
            base_url="https://api.meta.ai/v1",
            released_at=date(2026, 8, 5),
        ),
    },
    "deepseek": {
        # DeepSeek V4 via Responses (default; builtin web_search) with Chat Completions fallback.
        # Credentials: vault key "deepseek" or config deepseek_api_key
        # (MOTET_DEEPSEEK_API_KEY / DEEPSEEK_API_KEY). See https://api-docs.deepseek.com/
        # Pricing: per-1M → per-1k; cache_read_discount from cache-hit vs cache-miss input.
        "deepseek-v4-flash": ModelSpec(
            provider="deepseek",
            name="deepseek-v4-flash",
            display_name="DeepSeek V4 Flash",
            capabilities={
                CAP_STREAM,
                CAP_SYSTEM_PROMPT,
                CAP_TOOL_USE,
                CAP_JSON_MODE,
                CAP_REASONING,
            },
            max_output_tokens=384000,
            supported_adapters=["responses", "chat_completions"],
            default_adapter="responses",
            fallback_adapters=["chat_completions"],
            supported_builtin_tools=["deepseek.web_search"],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.00014"),
                output_per_1k=Decimal("0.00028"),
                cache_read_discount_pct=Decimal("98.0"),
            ),
            base_url="https://api.deepseek.com",
        ),
        "deepseek-v4-pro": ModelSpec(
            provider="deepseek",
            name="deepseek-v4-pro",
            display_name="DeepSeek V4 Pro",
            capabilities={
                CAP_STREAM,
                CAP_SYSTEM_PROMPT,
                CAP_TOOL_USE,
                CAP_JSON_MODE,
                CAP_REASONING,
            },
            max_output_tokens=384000,
            supported_adapters=["responses", "chat_completions"],
            default_adapter="responses",
            fallback_adapters=["chat_completions"],
            supported_builtin_tools=["deepseek.web_search"],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.000435"),
                output_per_1k=Decimal("0.00087"),
                cache_read_discount_pct=Decimal("99.17"),
            ),
            base_url="https://api.deepseek.com",
        ),
    },
    "gemini": {
        # Native generateContent API via GeminiGenerateContentAdapter (google-genai).
        # Model IDs match Google AI Studio / Gemini API (see https://ai.google.dev/gemini-api/docs/models).
        # Preview IDs change over time; update `name` when Google renames or promotes models.
        "gemini-2.5-flash": ModelSpec(
            provider="gemini",
            name="gemini-2.5-flash",
            display_name="Gemini 2.5 Flash",
            capabilities={
                CAP_STREAM,
                CAP_SYSTEM_PROMPT,
                CAP_TOOL_USE,
                CAP_JSON_MODE,
                CAP_VISION,
            },
            max_output_tokens=65536,
            supported_adapters=["generate_content"],
            default_adapter="generate_content",
            fallback_adapters=None,
            supported_builtin_tools=[],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.0003"),
                output_per_1k=Decimal("0.0025"),
            ),
        ),
        "gemini-2.5-flash-lite": ModelSpec(
            provider="gemini",
            name="gemini-2.5-flash-lite",
            display_name="Gemini 2.5 Flash-Lite",
            capabilities={
                CAP_STREAM,
                CAP_SYSTEM_PROMPT,
                CAP_TOOL_USE,
                CAP_JSON_MODE,
                CAP_VISION,
            },
            max_output_tokens=65536,
            supported_adapters=["generate_content"],
            default_adapter="generate_content",
            fallback_adapters=None,
            supported_builtin_tools=[],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.0001"),
                output_per_1k=Decimal("0.0004"),
            ),
        ),
        "gemini-2.5-pro": ModelSpec(
            provider="gemini",
            name="gemini-2.5-pro",
            display_name="Gemini 2.5 Pro",
            capabilities={
                CAP_STREAM,
                CAP_SYSTEM_PROMPT,
                CAP_TOOL_USE,
                CAP_JSON_MODE,
                CAP_VISION,
                CAP_REASONING,
            },
            max_output_tokens=65536,
            supported_adapters=["generate_content"],
            default_adapter="generate_content",
            fallback_adapters=None,
            supported_builtin_tools=[],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.00125"),
                output_per_1k=Decimal("0.01"),
            ),
        ),
        "gemini-3-flash-preview": ModelSpec(
            provider="gemini",
            name="gemini-3-flash-preview",
            display_name="Gemini 3 Flash (Preview)",
            capabilities={
                CAP_STREAM,
                CAP_SYSTEM_PROMPT,
                CAP_TOOL_USE,
                CAP_JSON_MODE,
                CAP_VISION,
                CAP_REASONING,
            },
            max_output_tokens=65536,
            supported_adapters=["generate_content"],
            default_adapter="generate_content",
            fallback_adapters=None,
            supported_builtin_tools=[],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.0005"),
                output_per_1k=Decimal("0.003"),
            ),
        ),
        "gemini-3.1-pro-preview": ModelSpec(
            provider="gemini",
            name="gemini-3.1-pro-preview",
            display_name="Gemini 3.1 Pro (Preview)",
            capabilities={
                CAP_STREAM,
                CAP_SYSTEM_PROMPT,
                CAP_TOOL_USE,
                CAP_JSON_MODE,
                CAP_VISION,
                CAP_REASONING,
            },
            max_output_tokens=65536,
            supported_adapters=["generate_content"],
            default_adapter="generate_content",
            fallback_adapters=None,
            supported_builtin_tools=[],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.002"),
                output_per_1k=Decimal("0.012"),
            ),
        ),
        "gemini-3.1-flash-lite-preview": ModelSpec(
            provider="gemini",
            name="gemini-3.1-flash-lite-preview",
            display_name="Gemini 3.1 Flash-Lite (Preview)",
            capabilities={
                CAP_STREAM,
                CAP_SYSTEM_PROMPT,
                CAP_TOOL_USE,
                CAP_JSON_MODE,
                CAP_VISION,
            },
            max_output_tokens=65536,
            supported_adapters=["generate_content"],
            default_adapter="generate_content",
            fallback_adapters=None,
            supported_builtin_tools=[],
            pricing=ModelPricing(
                input_per_1k=Decimal("0.0001"),
                output_per_1k=Decimal("0.0005"),
            ),
        ),
    },
    "local": {
        # Newer local models (GGUF/HuggingFace format). Paths resolved via inference_manager DEFAULT_MODEL_PATHS
        # or MOTET_LOCAL_MODEL_PATHS env. Streaming via Redis Streams.
        # Local models have pricing=None (free - run on local hardware).
        # Refreshed local tier (ADR-0117): current open-weight generations. Small
        # dense models for the day-to-day generative-UI tier plus one mid-size MoE
        # ("large" option) that stays interactive on 64GB Apple Silicon. GGUFs carry
        # embedded Jinja chat templates, used as the primary formatting path
        # (ADR-0117); the per-family pinned handler + stop tokens remain as a
        # fallback/safety net (ADR-0114).
        "gemma-4-e4b": ModelSpec(
            provider="local",
            name="gemma-4-e4b",
            display_name="Gemma 4 E4B Instruct",
            capabilities={
                CAP_STREAM,
                CAP_SYSTEM_PROMPT,
                CAP_STRUCTURED_OUTPUT,
                CAP_TOOL_USE,
                CAP_REASONING,
            },
            max_output_tokens=8192,
            supported_adapters=["local"],
            default_adapter="local",
            fallback_adapters=None,
            supported_builtin_tools=[],
            pricing=None,
            provenance=ModelProvenance(
                origin="us",
                vendor="Google",
                open_weights=True,
                license="Apache-2.0",
                hosting=ModelHosting.LOCAL,
            ),
        ),
        "gemma-4-26b-a4b": ModelSpec(
            provider="local",
            name="gemma-4-26b-a4b",
            display_name="Gemma 4 26B-A4B (MoE)",
            capabilities={
                CAP_STREAM,
                CAP_SYSTEM_PROMPT,
                CAP_STRUCTURED_OUTPUT,
                CAP_TOOL_USE,
            },
            max_output_tokens=8192,
            supported_adapters=["local"],
            default_adapter="local",
            fallback_adapters=None,
            supported_builtin_tools=[],
            pricing=None,
            provenance=ModelProvenance(
                origin="us",
                vendor="Google",
                open_weights=True,
                license="Apache-2.0",
                hosting=ModelHosting.LOCAL,
                notes="Mixture-of-experts (~26B total, ~3.8B active): big-model "
                "quality at small-model speed; the 'large' local option.",
            ),
        ),
        "hermes-4-14b": ModelSpec(
            provider="local",
            name="hermes-4-14b",
            display_name="Hermes 4 14B",
            # Hermes 4 has hybrid reasoning with <think> blocks and Hermes-style
            # <tool_call> markup, both handled by the local profile/parser.
            capabilities={
                CAP_STREAM,
                CAP_SYSTEM_PROMPT,
                CAP_STRUCTURED_OUTPUT,
                CAP_TOOL_USE,
                CAP_REASONING,
            },
            max_output_tokens=8192,
            supported_adapters=["local"],
            default_adapter="local",
            fallback_adapters=None,
            supported_builtin_tools=[],
            pricing=None,
            provenance=ModelProvenance(
                origin="us",
                vendor="Nous Research",
                open_weights=True,
                license="Apache-2.0",
                hosting=ModelHosting.LOCAL,
                notes="Hybrid reasoning and tool-focused finetune based on Qwen 3 14B.",
            ),
        ),
        "llama-3.1-8b-instruct": ModelSpec(
            provider="local",
            name="llama-3.1-8b-instruct",
            display_name="Llama 3.1 8B Instruct",
            # CAP_TOOL_USE: Llama 3.1 has native function calling (ADR-0115 Path B).
            capabilities={CAP_STREAM, CAP_SYSTEM_PROMPT, CAP_STRUCTURED_OUTPUT, CAP_TOOL_USE},
            max_output_tokens=8192,
            supported_adapters=["local"],
            default_adapter="local",
            fallback_adapters=None,
            supported_builtin_tools=[],
            pricing=None,
            provenance=ModelProvenance(
                origin="us",
                vendor="Meta",
                open_weights=True,
                license="Llama 3.1 Community",
                hosting=ModelHosting.LOCAL,
            ),
        ),
        "ministral-3-8b-instruct": ModelSpec(
            provider="local",
            name="ministral-3-8b-instruct",
            display_name="Ministral 3 8B Instruct",
            # CAP_TOOL_USE: Ministral 3 has native function calling (ADR-0115 Path B).
            capabilities={CAP_STREAM, CAP_SYSTEM_PROMPT, CAP_STRUCTURED_OUTPUT, CAP_TOOL_USE},
            max_output_tokens=8192,
            supported_adapters=["local"],
            default_adapter="local",
            fallback_adapters=None,
            supported_builtin_tools=[],
            pricing=None,
            provenance=ModelProvenance(
                origin="eu",
                vendor="Mistral AI",
                open_weights=True,
                license="Apache-2.0",
                hosting=ModelHosting.LOCAL,
                restrictions=frozenset({RestrictionTag.NON_US_ORIGIN}),
                notes="EU-origin (France). Native function calling / JSON output.",
            ),
        ),
        # US-origin generative-UI tier (ADR-0114): small, fast, provenance-clean
        # models that emit a compact DSL via grammar-constrained decoding. GGUFs
        # carry chat-template metadata, so create_chat_completion applies the
        # correct per-model format on the local path.
        "phi-4-mini": ModelSpec(
            provider="local",
            name="phi-4-mini",
            display_name="Phi-4 Mini Instruct",
            # Tool use via ADR-0115 (system-message injection): phi-4's template
            # reads tools from the system message, not the native tools= channel.
            capabilities={CAP_STREAM, CAP_SYSTEM_PROMPT, CAP_STRUCTURED_OUTPUT, CAP_TOOL_USE},
            max_output_tokens=4096,
            supported_adapters=["local"],
            default_adapter="local",
            fallback_adapters=None,
            supported_builtin_tools=[],
            pricing=None,
            provenance=ModelProvenance(
                origin="us",
                vendor="Microsoft",
                open_weights=True,
                license="MIT",
                hosting=ModelHosting.LOCAL,
            ),
        ),
        "gemma-3-4b": ModelSpec(
            provider="local",
            name="gemma-3-4b",
            display_name="Gemma 3 4B Instruct",
            capabilities={CAP_STREAM, CAP_SYSTEM_PROMPT, CAP_STRUCTURED_OUTPUT},
            max_output_tokens=8192,
            supported_adapters=["local"],
            default_adapter="local",
            fallback_adapters=None,
            supported_builtin_tools=[],
            pricing=None,
            provenance=ModelProvenance(
                origin="us",
                vendor="Google",
                open_weights=True,
                license="Gemma",
                hosting=ModelHosting.LOCAL,
            ),
        ),
        # CN-origin (Alibaba), open-weight. Strong instruction-following and
        # structured output; runs locally (no egress). Provenance-gated for
        # restricted deployments via its restriction tags (ADR-0115/0116).
        "qwen3-8b-instruct": ModelSpec(
            provider="local",
            name="qwen3-8b-instruct",
            display_name="Qwen3 8B Instruct",
            # CAP_REASONING: Qwen3 emits <think> chain-of-thought (separated by the
            # local adapter). CAP_TOOL_USE: Qwen3 has native function calling
            # (ADR-0115 Path B). Both are capability-gated with graceful degradation.
            capabilities={CAP_STREAM, CAP_SYSTEM_PROMPT, CAP_STRUCTURED_OUTPUT, CAP_TOOL_USE, CAP_REASONING},
            max_output_tokens=8192,
            supported_adapters=["local"],
            default_adapter="local",
            fallback_adapters=None,
            supported_builtin_tools=[],
            pricing=None,
            provenance=ModelProvenance(
                origin="cn",
                vendor="Alibaba",
                open_weights=True,
                license="Apache-2.0",
                hosting=ModelHosting.LOCAL,
                restrictions=frozenset({
                    RestrictionTag.FOREIGN_ADVERSARY_ORIGIN,
                    RestrictionTag.NON_US_ORIGIN,
                }),
                notes="Chinese-origin (Alibaba). Runs locally so no data egress; "
                "gated from provenance-restricted deployments by origin.",
            ),
        ),
    },
}


# ADR-0116: Provider-default provenance for hosted/cloud models.
#
# Cloud provenance is uniform per provider (every OpenAI model is US/OpenAI, etc.),
# unlike the heterogeneous local tier (Meta/Microsoft/Google/Mistral), which is
# annotated explicitly per model above. We therefore record cloud provenance once
# per provider and apply it to any spec that does not already declare its own,
# which also auto-covers future cloud models.
#
# Important: for hosted APIs, acceptability in a provenance-restricted deployment
# is dominated by *data egress* (your data leaves the trust boundary to a third
# party), not just model origin. So US-origin cloud models are NOT marked
# restricted_ok=True the way US-origin *local* models are; they are left undecided
# (None) for a future deployment policy/operator to resolve. The actual gate is a
# follow-on (ADR-0116); this is storage only.
_PROVIDER_DEFAULT_PROVENANCE: Dict[str, ModelProvenance] = {
    "openai": ModelProvenance(
        origin="us",
        vendor="OpenAI",
        open_weights=False,
        license="Proprietary",
        hosting=ModelHosting.HOSTED_API,
        restrictions=frozenset({RestrictionTag.DATA_EGRESS, RestrictionTag.CLOSED_WEIGHTS}),
        notes="US-origin hosted API; closed weights.",
    ),
    "anthropic": ModelProvenance(
        origin="us",
        vendor="Anthropic",
        open_weights=False,
        license="Proprietary",
        hosting=ModelHosting.HOSTED_API,
        restrictions=frozenset({RestrictionTag.DATA_EGRESS, RestrictionTag.CLOSED_WEIGHTS}),
        notes="US-origin hosted API; closed weights.",
    ),
    "gemini": ModelProvenance(
        origin="us",
        vendor="Google",
        open_weights=False,
        license="Proprietary",
        hosting=ModelHosting.HOSTED_API,
        restrictions=frozenset({RestrictionTag.DATA_EGRESS, RestrictionTag.CLOSED_WEIGHTS}),
        notes="US-origin hosted API; closed weights.",
    ),
    "xai": ModelProvenance(
        origin="us",
        vendor="xAI",
        open_weights=False,
        license="Proprietary",
        hosting=ModelHosting.HOSTED_API,
        restrictions=frozenset({RestrictionTag.DATA_EGRESS, RestrictionTag.CLOSED_WEIGHTS}),
        notes="US-origin hosted API (SpaceXAI / xAI); closed weights.",
    ),
    "meta": ModelProvenance(
        origin="us",
        vendor="Meta",
        open_weights=False,
        license="Proprietary",
        hosting=ModelHosting.HOSTED_API,
        restrictions=frozenset({RestrictionTag.DATA_EGRESS, RestrictionTag.CLOSED_WEIGHTS}),
        notes="US-origin hosted API (Meta Model API / Muse Spark); closed weights.",
    ),
    "moonshot": ModelProvenance(
        origin="cn",
        vendor="Moonshot AI",
        open_weights=False,
        license="Proprietary",
        hosting=ModelHosting.HOSTED_API,
        restrictions=frozenset({
            RestrictionTag.FOREIGN_ADVERSARY_ORIGIN,
            RestrictionTag.NON_US_ORIGIN,
            RestrictionTag.DATA_EGRESS,
            RestrictionTag.CLOSED_WEIGHTS,
        }),
        notes="Chinese-origin hosted API (Moonshot AI, Beijing).",
    ),
    "deepseek": ModelProvenance(
        origin="cn",
        vendor="DeepSeek",
        open_weights=False,
        license="Proprietary",
        hosting=ModelHosting.HOSTED_API,
        restrictions=frozenset({
            RestrictionTag.FOREIGN_ADVERSARY_ORIGIN,
            RestrictionTag.NON_US_ORIGIN,
            RestrictionTag.DATA_EGRESS,
            RestrictionTag.CLOSED_WEIGHTS,
        }),
        notes="Chinese-origin hosted API (DeepSeek).",
    ),
}


def _apply_default_provenance(registry: Dict[str, Dict[str, "ModelSpec"]]) -> None:
    """Fill provider-default provenance for any spec that lacks its own (ADR-0116).

    Explicit per-model provenance (e.g. the local tier) is preserved; only specs
    with ``provenance is None`` whose provider has a default are updated. Specs are
    frozen, so we rebuild them via ``model_copy``.
    """
    for provider, models in registry.items():
        default = _PROVIDER_DEFAULT_PROVENANCE.get(provider)
        if default is None:
            continue
        for name, spec in list(models.items()):
            if spec.provenance is None:
                models[name] = spec.model_copy(update={"provenance": default})


# Best-effort public launch dates keyed by (provider, registry_key).
# Prefer setting ``released_at=`` on new ModelSpec constructors; this map backfills
# existing entries and is skipped when the constructor already set a date.
# Sources: provider announcements / docs comments in this file; day precision when known,
# otherwise first-of-month approximations.
_MODEL_RELEASED_AT: Dict[Tuple[str, str], date] = {
    # mock
    ("mock", "mock-small"): date(2024, 1, 1),
    # openai — chat
    ("openai", "gpt-4o"): date(2024, 5, 13),
    ("openai", "gpt-4o-mini"): date(2024, 7, 18),
    ("openai", "gpt-4o-mini-chat"): date(2024, 7, 18),
    ("openai", "o3-mini"): date(2025, 1, 31),
    ("openai", "gpt-4.1"): date(2025, 4, 14),
    ("openai", "gpt-4.1-mini"): date(2025, 4, 14),
    ("openai", "gpt-4.1-mini-chat"): date(2025, 4, 14),
    ("openai", "gpt-4.1-nano"): date(2025, 4, 14),
    ("openai", "o3"): date(2025, 4, 16),
    ("openai", "gpt-5"): date(2025, 8, 7),
    ("openai", "gpt-5-mini"): date(2025, 8, 7),
    ("openai", "gpt-5-nano"): date(2025, 8, 7),
    ("openai", "gpt-5.1"): date(2025, 11, 13),
    ("openai", "gpt-5.2"): date(2025, 12, 11),
    ("openai", "gpt-5.4"): date(2026, 3, 5),
    ("openai", "gpt-5.4-mini"): date(2026, 3, 5),
    ("openai", "gpt-5.4-nano"): date(2026, 3, 5),
    ("openai", "gpt-5.5"): date(2026, 4, 23),
    # openai — image
    ("openai", "dall-e-3"): date(2023, 10, 1),
    ("openai", "gpt-image-1"): date(2025, 3, 25),
    ("openai", "gpt-image-1.5"): date(2025, 12, 16),
    ("openai", "gpt-image-2"): date(2026, 4, 21),
    # anthropic — dates from GET /v1/models `released_at` (2026-07-23)
    ("anthropic", "claude-sonnet-4-20250514"): date(2025, 5, 14),
    ("anthropic", "claude-opus-4-20250514"): date(2025, 5, 14),
    ("anthropic", "claude-opus-4-1-20250805"): date(2025, 8, 5),
    ("anthropic", "claude-sonnet-4-5-20250929"): date(2025, 9, 29),
    ("anthropic", "claude-sonnet-4.5"): date(2025, 9, 29),
    ("anthropic", "claude-haiku-4-5-20251001"): date(2025, 10, 15),
    ("anthropic", "claude-opus-4-5-20251101"): date(2025, 11, 24),
    ("anthropic", "claude-sonnet-4-6"): date(2026, 2, 17),
    ("anthropic", "claude-sonnet-4.6"): date(2026, 2, 17),
    ("anthropic", "claude-opus-4-6"): date(2026, 2, 4),
    ("anthropic", "claude-opus-4-7"): date(2026, 4, 14),
    ("anthropic", "claude-opus-4.7"): date(2026, 4, 14),
    # moonshot
    ("moonshot", "kimi-k2-0905-preview"): date(2025, 9, 5),
    ("moonshot", "kimi-k2-thinking"): date(2025, 11, 6),
    ("moonshot", "kimi-k2.5"): date(2026, 1, 27),
    ("moonshot", "kimi-k2.7-code"): date(2026, 6, 1),
    ("moonshot", "kimi-k3"): date(2026, 7, 1),
    # xai
    ("xai", "grok-4.5"): date(2026, 6, 1),
    ("xai", "grok-4.6"): date(2026, 8, 12),
    # meta — Muse Spark 1.2 announced 2026-08-05; 1.1 is the earlier checkpoint
    ("meta", "muse-spark-1.1"): date(2026, 7, 1),
    ("meta", "muse-spark-1.2"): date(2026, 8, 5),
    # deepseek
    ("deepseek", "deepseek-v4-flash"): date(2026, 7, 1),
    ("deepseek", "deepseek-v4-pro"): date(2026, 7, 1),
    # gemini
    ("gemini", "gemini-2.5-flash"): date(2025, 5, 20),
    ("gemini", "gemini-2.5-pro"): date(2025, 5, 20),
    ("gemini", "gemini-2.5-flash-lite"): date(2025, 7, 22),
    ("gemini", "gemini-3-flash-preview"): date(2025, 12, 1),
    ("gemini", "gemini-3.1-pro-preview"): date(2026, 2, 1),
    ("gemini", "gemini-3.1-flash-lite-preview"): date(2026, 3, 1),
    # local (open-weight publish / Motet registry add — approximate)
    ("local", "llama-3.1-8b-instruct"): date(2024, 7, 23),
    ("local", "phi-4-mini"): date(2025, 2, 26),
    ("local", "gemma-3-4b"): date(2025, 3, 12),
    ("local", "qwen3-8b-instruct"): date(2025, 4, 29),
    ("local", "ministral-3-8b-instruct"): date(2025, 12, 2),
    ("local", "hermes-4-14b"): date(2025, 8, 25),
    ("local", "gemma-4-e4b"): date(2026, 4, 1),
    ("local", "gemma-4-26b-a4b"): date(2026, 4, 1),
}


def _apply_released_at(registry: Dict[str, Dict[str, "ModelSpec"]]) -> None:
    """Fill ``released_at`` from ``_MODEL_RELEASED_AT`` when the spec left it unset."""
    for (provider, key), released in _MODEL_RELEASED_AT.items():
        models = registry.get(provider)
        if not models or key not in models:
            continue
        spec = models[key]
        if spec.released_at is None:
            models[key] = spec.model_copy(update={"released_at": released})


# ADR-0124: hosted chat/completions providers that participate in prompt caching.
# Image-only specs (CAP_IMAGE_GENERATION without CAP_STREAM) are skipped.
_PROMPT_CACHING_PROVIDERS = frozenset({"openai", "anthropic", "xai", "moonshot", "deepseek", "meta"})


def _apply_prompt_caching_capability(registry: Dict[str, Dict[str, "ModelSpec"]]) -> None:
    """Tag eligible hosted text models with CAP_PROMPT_CACHING (ADR-0124).

    Explicit per-model capabilities that already include the cap are preserved.
    Image-generation-only and non-eligible providers are left unchanged.
    """
    for provider, models in registry.items():
        if provider not in _PROMPT_CACHING_PROVIDERS:
            continue
        for name, spec in list(models.items()):
            caps = set(spec.capabilities or set())
            if CAP_PROMPT_CACHING in caps:
                continue
            # Skip image-only models (no streaming chat path).
            if CAP_IMAGE_GENERATION in caps and CAP_STREAM not in caps:
                continue
            if CAP_STREAM not in caps:
                continue
            caps.add(CAP_PROMPT_CACHING)
            models[name] = spec.model_copy(update={"capabilities": caps})


_apply_default_provenance(MODEL_REGISTRY)
_apply_released_at(MODEL_REGISTRY)
_apply_prompt_caching_capability(MODEL_REGISTRY)


__all__ = [
    "ModelSpec",
    "ModelProvenance",
    "ModelHosting",
    "RestrictionTag",
    "MODEL_REGISTRY",
    "CAP_STREAM",
    "CAP_VISION",
    "CAP_TOOL_USE",
    "CAP_JSON_MODE",
    "CAP_SYSTEM_PROMPT",
    "CAP_REASONING",
    "CAP_IMAGE_GENERATION",
    "CAP_STRUCTURED_OUTPUT",
    "CAP_PROMPT_CACHING",
]

