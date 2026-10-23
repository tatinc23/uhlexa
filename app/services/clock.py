"""Strictly increasing UTC timestamps for rows whose order is part of the product (activity, messages, tasks).

`utcnow()` can repeat within a microsecond on a fast machine, which would make "newest first" nondeterministic.
"""

import threading
from datetime import datetime, timedelta

from app.db.base import utcnow

_lock = threading.Lock()
_last: datetime | None = None


def tick() -> datetime:
    global _last
    with _lock:
        now = utcnow()
        if _last is not None and now <= _last:
            now = _last + timedelta(microseconds=1)
        _last = now
        return now
