"""Backwards-compatible config import.

Use `shared.idp_common.config.get_settings` from service code.
"""

from shared.idp_common.config import Settings, get_settings

__all__ = ["Settings", "get_settings"]
