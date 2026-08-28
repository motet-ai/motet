"""
Motet - Tenancy Package

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-24

Description:
    Tenant and Motet (environment) catalog primitives for the control plane.
    See tenant_registry.py for the Redis-backed registry used by the tenants API
    and manage-app scope selector.

Dependencies:
    - motet.core.tenancy.tenant_registry: catalog CRUD

Usage:
    from motet.core.tenancy import TenantRegistry

Notes:
    - Distinct from JWT tenant remapping and ScopedRegistry grants
"""

from .tenant_registry import (
    ALL_TENANTS,
    MotetConflictError,
    MotetNotFoundError,
    MotetRecord,
    TenantConflictError,
    TenantNotFoundError,
    TenantRecord,
    TenantRegistry,
    TenantRegistryError,
    TenantValidationError,
    validate_catalog_id,
)

__all__ = [
    "ALL_TENANTS",
    "MotetConflictError",
    "MotetNotFoundError",
    "MotetRecord",
    "TenantConflictError",
    "TenantNotFoundError",
    "TenantRecord",
    "TenantRegistry",
    "TenantRegistryError",
    "TenantValidationError",
    "validate_catalog_id",
]
