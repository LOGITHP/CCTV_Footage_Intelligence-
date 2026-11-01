"""Replaceable infrastructure adapters for the video intelligence service.

The API's default path remains the local SQLite/filesystem implementation.  The
adapters in this package are selected explicitly from environment settings so a
developer can run the full test suite without PostgreSQL, object storage,
Qdrant, or Redis running.
"""

from .capabilities import infrastructure_capabilities
from .config import InfrastructureSettings, load_settings

__all__ = ["InfrastructureSettings", "load_settings", "infrastructure_capabilities"]
