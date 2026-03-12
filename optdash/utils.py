"""Shared utility functions used across analytics modules.

Issue-9: extracted duplicated helpers to a single location to prevent
future divergence between independent copies in different modules.
"""


def snap_to_min(t: str) -> int:
    """Convert 'HH:MM' (or 'HH:MM:SS') to integer minutes-since-midnight.

    Uses t[:5] slicing so both 'HH:MM' and 'HH:MM:SS' formats work.
    Returns 9*60+15 (market open) on any parse failure so callers never
    receive an uninitialized value from a malformed snap_time string.
    """
    try:
        h, m = map(int, t[:5].split(":"))
        return h * 60 + m
    except Exception:
        return 9 * 60 + 15   # fallback: treat as market open
