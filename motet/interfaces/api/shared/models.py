"""
Motet - Shared API Models

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2025-11-14

Description:
    Shared Pydantic models used across multiple API endpoints. Models that are
    used by more than one API should be placed here to avoid duplication.

Dependencies:
    - pydantic: Data validation and serialization

Usage:
    from motet.interfaces.api.shared.models import SharedModel
    
    @router.post("/endpoint")
    async def my_endpoint(data: SharedModel):
        # Use shared model
        pass

Notes:
    - Currently empty - add shared models as they are identified
    - Models used by only one API should remain in that API's file
"""

# Shared Pydantic models will be added here as needed
# Example:
# from pydantic import BaseModel, Field
# 
# class SharedRequestModel(BaseModel):
#     """Shared request model used by multiple APIs."""
#     field: str = Field(..., description="Field description")

