"""
Motet - OpenAI Compatible API Facade

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Inbound OpenAI-compatible HTTP facade for Motet.

    Lets any client that speaks the OpenAI API — Cursor, Open WebUI, the OpenAI
    SDKs, LangChain — use Motet by pointing at a base URL and supplying a Motet
    service account token as the API key. Requests run through Motet's existing
    distributed commands, so registry routing, vault credentials, tenant model
    profiles, budgets, command events, and traces all apply.

    Three execution modes back the same routes: passthrough (models only),
    hosted_tools (bounded server-side Motet tool execution), and agent (the full
Motet agent stack, including memory and artifact RAG).

Dependencies:
    - fastapi: routing and streaming responses
    - motet.core.security.facade_policy: per-credential mode and model policy

Usage:
    from motet.interfaces.api.openai_compat import router

    app.include_router(router, prefix="/v1")

Notes:
    - Disabled unless MOTET_OPENAI_COMPAT_ENABLED is set
    - Model access is deny-by-default; see README.md in this package
    - The router carries no prefix so the mount point stays configurable
"""

from .routes import router

__all__ = ["router"]
