"""Small persistent cache of public identifier evidence, not portfolio values."""

import json
import logging
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from trading212_mcp.market.normalization import is_mapping


class MappingCache:
    def __init__(self, path: Path, ttl_days: int) -> None:
        self.path = path
        self.ttl_seconds = ttl_days * 86400

    def get(self, key: str) -> dict[str, object] | None:
        if not self.path.exists():
            return None
        try:
            with closing(sqlite3.connect(self.path, timeout=1)) as db, db:
                row = db.execute(
                    "SELECT value FROM mappings WHERE key=? AND expires>?",
                    (key, time.time()),
                ).fetchone()
            value = json.loads(row[0]) if row else None
            return value if is_mapping(value) else None
        except (sqlite3.Error, OSError, ValueError):
            logging.getLogger(__name__).warning(
                "Instrument cache could not be read; identifiers will be revalidated."
            )
            return None

    def put(self, key: str, value: dict[str, object]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.path, timeout=1)) as db, db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS mappings (key TEXT PRIMARY KEY, value TEXT NOT NULL, expires REAL NOT NULL, updated REAL NOT NULL)"
                )
                now = time.time()
                db.execute(
                    "INSERT INTO mappings VALUES (?,?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, expires=excluded.expires, updated=excluded.updated",
                    (
                        key,
                        json.dumps(value, allow_nan=False),
                        now + self.ttl_seconds,
                        now,
                    ),
                )
                db.execute("DELETE FROM mappings WHERE expires<=?", (now,))
                db.execute(
                    "DELETE FROM mappings WHERE key IN (SELECT key FROM mappings ORDER BY updated DESC LIMIT -1 OFFSET 5000)"
                )
        except (sqlite3.Error, OSError, ValueError):
            logging.getLogger(__name__).warning(
                "Instrument cache could not be saved; this mapping will need revalidation after restart."
            )
