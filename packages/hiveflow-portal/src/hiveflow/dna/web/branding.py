"""HiveFlowAI branding assets.

The S3-backed branding override (bucket + symbol/logo object keys) is retired —
those artifacts were the pre-rebrand logo and have been moved to a legacy
folder; the bucket itself may be deleted later. Branding is always served from
the bundled static files now.
"""

from __future__ import annotations


def load_branding_asset(filename: str) -> bytes | None:
    """Always None — branding is served from the bundled static files."""
    return None
