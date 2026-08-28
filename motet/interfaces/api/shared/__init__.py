"""Shared utilities for Motet APIs."""
from .auth import get_current_principal
from .identity import apply_principal_to_config, attach_principal_to_stack, get_principal_context
from .models import *  # noqa: F401, F403
from .dependencies import *  # noqa: F401, F403
from .scope import ManageAppScope, get_manage_app_scope, matches_scope

__all__ = [
    "get_current_principal",
    "apply_principal_to_config",
    "attach_principal_to_stack",
    "get_principal_context",
    "ManageAppScope",
    "get_manage_app_scope",
    "matches_scope",
]

