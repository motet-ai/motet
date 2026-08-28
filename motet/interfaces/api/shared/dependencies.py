"""
Motet - Shared API Dependencies

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2025-11-14

Description:
    Shared FastAPI dependencies used across multiple API endpoints. Dependencies
    that are used by more than one API should be placed here to avoid duplication.

Dependencies:
    - fastapi: Web framework for REST API

Usage:
    from motet.interfaces.api.shared.dependencies import shared_dependency
    
    @router.get("/endpoint")
    async def my_endpoint(value: str = Depends(shared_dependency)):
        # Use shared dependency
        pass

Notes:
    - Currently empty - add shared dependencies as they are identified
    - Dependencies used by only one API should remain in that API's file
"""

# Shared FastAPI dependencies will be added here as needed
# Example:
# from fastapi import Depends
# 
# async def shared_dependency():
#     """Shared dependency used by multiple APIs."""
#     return "value"

