"""
Motet - CLI Entry Point

Delegates to motet_sdk.cli so the full CLI lives in the SDK package.
Ensure motet-sdk is installed (pip install -e motet-sdk) when using the repo.
"""

from motet_sdk.cli import main_group

__all__ = ["main_group"]

if __name__ == "__main__":
    main_group()
