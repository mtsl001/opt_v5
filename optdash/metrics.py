"""Lightweight in-memory analytics error counters.

Issue-11: analytics modules catch Exception and return empty results to
keep the scheduler running.  These counters let the /health endpoint
surface how often each function is silently failing so operational issues
(e.g. DuckDB schema changes, corrupted data) are detectable without
searching through log files.

Usage in analytics modules:
    from optdash.metrics import record_error
    except Exception as e:
        record_error("get_net_gex")
        logger.warning(...)
        return {}

Usage in /health endpoint:
    from optdash.metrics import get_error_counts
    return {"status": "ok", "analytics_errors": get_error_counts()}
"""
from collections import defaultdict
import threading

_lock = threading.Lock()
_counts: dict[str, int] = defaultdict(int)


def record_error(function_name: str) -> None:
    """Increment the error counter for the given analytics function."""
    with _lock:
        _counts[function_name] += 1


def get_error_counts() -> dict[str, int]:
    """Return a snapshot of all error counts (for /health endpoint)."""
    with _lock:
        return dict(_counts)
