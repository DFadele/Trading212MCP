import logging
import sqlite3
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from trading212_mcp.market.disk_cache import DiskCache


@dataclass(frozen=True)
class CachedResponse:
    rows: list[dict[str, object]]
    retrieved_at: datetime


class TTLCache:
    """Bounded in-memory cache. Timestamps describe retrieval, not cache hits."""

    def __init__(
        self,
        ttl: int,
        max_entries: int,
        path: Path | None = None,
        namespace: str = "public",
    ) -> None:
        self.disk = DiskCache(path, namespace, max_entries) if path else None
        self.ttl = ttl
        self.max_entries = max_entries
        self._entries: OrderedDict[str, tuple[float, CachedResponse]] = OrderedDict()

    def get(self, key: str) -> CachedResponse | None:
        entry = self._entries.get(key)
        if entry is None:
            if self.disk is not None and self.ttl > 0:
                try:
                    stored = self.disk.get(key, self.ttl)
                    if stored is not None:
                        value = CachedResponse(stored[0], stored[1])
                        self._remember(key, value, min(stored[2], self.ttl))
                        return value
                except (
                    OSError,
                    sqlite3.Error,
                    ValueError,
                    TypeError,
                    KeyError,
                    IndexError,
                    RecursionError,
                ):
                    logging.getLogger(__name__).warning(
                        "Public cache read failed; refetching provider data."
                    )
            return None
        if entry[0] <= time.monotonic():
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return entry[1]

    def put(self, key: str, value: CachedResponse) -> None:
        if self.disk is not None and self.ttl > 0:
            try:
                self.disk.put(key, value.rows, value.retrieved_at, self.ttl)
            except (OSError, sqlite3.Error, ValueError, TypeError, RecursionError):
                logging.getLogger(__name__).warning(
                    "Public cache write failed; retaining memory cache only."
                )
        self._remember(key, value, self.ttl)

    def _remember(self, key: str, value: CachedResponse, remaining: float) -> None:
        self._entries[key] = (time.monotonic() + remaining, value)
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
