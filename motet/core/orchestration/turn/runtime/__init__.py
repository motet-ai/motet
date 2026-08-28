"""
Motet - Turn Runtime

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Turn Runtime package: the only owner of suspend, budget, and
    handback state. Public commands stay next to this package:

        agent_turn.py           ``agent_turn`` — start a turn (hooks + loop)
        resume_agent_turn.py    ``resume_agent_turn`` — public resume command

    This package is the private owner those commands call:

        persist.py   checkpoint writes, loop-head cancel, facade handles
        start.py     fresh-turn start + budget Continue (in-process)
        resume.py    in-process re-entry (load, validate, rebind, loop)
        result.py    TurnResult / ResumeHandle types

    Hosted_tools dispatches ``core.agent_loop`` with a fixed allowlist; it does
    not own a second loop.

Dependencies:
    - persist: store_turn_checkpoint allowlist lives here
    - resume: loop re-entry after a HANDBACK checkpoint
    - result: typed turn outcomes

Usage:
    from motet.core.orchestration.turn.runtime import (
        start, continue_after_budget, resume_turn,
        materialize_intent, resolve_resume,
    )

Notes:
    - Callers import this package, not persist.py / resume.py directly.
    - openai_compat must not import motet.core.checkpoints.
    - agentic_loop must not import this package (intents travel through the driver).
    - agent_turn must not import run_agentic_loop; it calls start / continue_after_budget.
"""

from motet.core.orchestration.turn.runtime.persist import (
    materialize_intent,
    persist_budget_continue_checkpoint,
    raise_if_turn_cancelled,
    resolve_resume,
)
from motet.core.orchestration.turn.runtime.resume import (
    ResumeTurnData,
    build_resume_history,
    resume_turn,
)
from motet.core.orchestration.turn.runtime.result import (
    ResumeHandle,
    TurnResult,
    TurnResultKind,
    coerce_turn_result,
    turn_result_from_loop_payload,
)
from motet.core.orchestration.turn.runtime.start import (
    continue_after_budget,
    start,
)

__all__ = [
    "ResumeHandle",
    "ResumeTurnData",
    "TurnResult",
    "TurnResultKind",
    "build_resume_history",
    "coerce_turn_result",
    "continue_after_budget",
    "materialize_intent",
    "persist_budget_continue_checkpoint",
    "raise_if_turn_cancelled",
    "resolve_resume",
    "resume_turn",
    "start",
    "turn_result_from_loop_payload",
]
