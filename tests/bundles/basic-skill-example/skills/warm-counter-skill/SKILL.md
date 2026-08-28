---
name: warm-counter-skill
description: Stateful lifetime demo skill — proves that module-level state persists across runner calls within a conversation.
---

# Warm Counter Skill

This skill demonstrates ADR-0106 ``lifetime: stateful`` runners. It exposes one
runner, ``counter``, whose script defines a ``handle(params)`` function and
keeps a counter in module-level state. Because stateful runners import the
skill module once into a long-lived in-container Python process, the
counter survives across calls *within the same conversation* — exactly
the contract authors get when they declare ``lifetime: stateful``.

## How To Use

Call the runner repeatedly within one conversation; each response will
include an incrementing ``count`` field. Calls in *different*
conversations get *different* counters, because each conversation gets
its own stateful container.

## Expected Behavior

First call (params: ``{"label": "first"}``):

```json
{"ok": true, "result": {"label": "first", "count": 1, "history": ["first"]}}
```

Second call (params: ``{"label": "second"}``):

```json
{"ok": true, "result": {"label": "second", "count": 2, "history": ["first", "second"]}}
```

If the counter resets to 1 between calls, the warm path is **not** active
(check that ``MOTET_WORKSPACE_CONTAINER_ENABLED=true``, the worker has
Docker socket access, and ``runners.yaml`` declares ``lifetime: stateful``).
