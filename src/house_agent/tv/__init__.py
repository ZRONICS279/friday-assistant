"""Brand-agnostic TV control."""

from .base import (
    CANONICAL_KEYS,
    TVApp,
    TVAuthRequired,
    TVClient,
    TVError,
    TVUnsupported,
    normalize_key,
)
from .registry import PLATFORMS, client_for, default_port, discover, normalize_platform

__all__ = [
    "CANONICAL_KEYS",
    "PLATFORMS",
    "TVApp",
    "TVAuthRequired",
    "TVClient",
    "TVError",
    "TVUnsupported",
    "client_for",
    "default_port",
    "discover",
    "normalize_key",
    "normalize_platform",
]
