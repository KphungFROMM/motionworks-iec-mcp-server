"""Parse-result cache keyed on the file's identity, not just its path.

Studio-scale projects are parsed on every tool call, so an unbounded re-parse
would dominate latency. We key on ``(path, mtime_ns, size)`` so an edited or
re-exported file is picked up automatically without any explicit invalidation
step — a stale cache would be far worse than a slow one.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Iterable

_MAX_ENTRIES = 24


class ParseCache:
    """Small thread-safe LRU over parsed projects."""

    def __init__(self, max_entries: int = _MAX_ENTRIES) -> None:
        self._max = max_entries
        self._lock = threading.RLock()
        self._data: OrderedDict[Any, Any] = OrderedDict()

    @staticmethod
    def signature(paths: Iterable[str | Path]) -> tuple:
        """Build a cache key from every file that contributed to a parse."""

        sig = []
        for p in paths:
            path = Path(p)
            try:
                st = path.stat()
                sig.append((str(path), st.st_mtime_ns, st.st_size))
            except OSError:
                sig.append((str(path), None, None))
        return tuple(sorted(sig))

    def get(self, key: Any) -> Any | None:
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
                return self._data[key]
        return None

    def put(self, key: Any, value: Any) -> Any:
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)
        return value

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


def cached(cache: ParseCache, key: Any, factory: Callable[[], Any]) -> Any:
    """Return the cached value for ``key``, computing it via ``factory`` once."""

    hit = cache.get(key)
    if hit is not None:
        return hit
    return cache.put(key, factory())
