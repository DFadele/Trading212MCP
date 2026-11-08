"""Versioned JSON cache codec for public vendor data; never unpickle disk files."""

import json
import math
import sqlite3
import time
from contextlib import closing
from datetime import date, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd

from trading212_mcp.market.normalization import is_list, is_mapping


def encode(value: Any) -> Any:
    if isinstance(value, pd.DataFrame):
        return [
            "frame",
            encode(list(value.index)),
            encode(list(value.columns)),
            encode(value.to_numpy(dtype=object).tolist()),
            {
                "index_datetime": isinstance(value.index, pd.DatetimeIndex),
                "index_tz": str(value.index.tz)
                if isinstance(value.index, pd.DatetimeIndex)
                and value.index.tz is not None
                else None,
                "columns_datetime": isinstance(value.columns, pd.DatetimeIndex),
                "columns_tz": str(value.columns.tz)
                if isinstance(value.columns, pd.DatetimeIndex)
                and value.columns.tz is not None
                else None,
                "index_name": encode(value.index.name),
                "columns_name": encode(value.columns.name),
            },
        ]
    if isinstance(value, pd.Timestamp):
        return ["timestamp", value.isoformat()]
    if isinstance(value, (datetime, date)):
        return [
            "datetime" if isinstance(value, datetime) else "date",
            value.isoformat(),
        ]
    if isinstance(value, np.generic):
        return encode(value.item())
    if isinstance(value, dict):
        return [
            "dict",
            [[k, encode(v)] for k, v in cast(dict[str, Any], value).items()],
        ]
    if isinstance(value, (list, tuple)):
        return [
            "tuple" if isinstance(value, tuple) else "list",
            [encode(v) for v in cast(list[Any] | tuple[Any, ...], value)],
        ]
    if isinstance(value, float) and not math.isfinite(value):
        return None  # DataFrame missing cells, never a synthetic financial zero.
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise TypeError("Unsupported public cache value")


def decode(value: Any) -> Any:
    if not isinstance(value, list):
        return value
    parts = cast(list[Any], value)
    tag = parts[0]
    if tag == "frame":
        metadata = parts[4]
        axes: list[Any] = []
        for name, raw_axis in (("index", parts[1]), ("columns", parts[2])):
            axis = decode(raw_axis)
            if metadata[name + "_datetime"]:
                zone = metadata[name + "_tz"]
                axis = (
                    pd.to_datetime(cast(list[Any], axis), utc=True).tz_convert(zone)
                    if zone
                    else pd.DatetimeIndex(axis)
                )
            axes.append(axis)
        frame = pd.DataFrame(decode(parts[3]), index=axes[0], columns=axes[1])
        frame.index.name, frame.columns.name = (
            decode(metadata["index_name"]),
            decode(metadata["columns_name"]),
        )
        return frame
    if tag == "dict":
        return {key: decode(v) for key, v in parts[1]}
    if tag in ("list", "tuple"):
        result = [decode(v) for v in parts[1]]
        return tuple(result) if tag == "tuple" else result
    if tag == "timestamp":
        return pd.Timestamp(parts[1])
    if tag == "datetime":
        return datetime.fromisoformat(parts[1])
    if tag == "date":
        return date.fromisoformat(parts[1])
    raise ValueError("Unknown public cache format")


class DiskCache:
    def __init__(self, path: Path, namespace: str, limit: int) -> None:
        self.path, self.namespace, self.limit = path, namespace, limit

    def connection(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=1)
        db.execute(
            "CREATE TABLE IF NOT EXISTS public_cache (namespace TEXT, key TEXT, value TEXT NOT NULL, expires REAL NOT NULL, updated REAL NOT NULL, PRIMARY KEY(namespace,key))"
        )
        return db

    def get(
        self, key: str, ttl: int
    ) -> tuple[list[dict[str, object]], datetime, float] | None:
        if not self.path.exists():
            return None
        with closing(self.connection()) as db:
            row = db.execute(
                "SELECT value,expires,updated FROM public_cache WHERE namespace=? AND key=? AND expires>?",
                (self.namespace, sha256(key.encode()).hexdigest(), time.time()),
            ).fetchone()
        if not row:
            return None
        expiry = min(row[1], row[2] + ttl)
        if expiry <= time.time():
            return None
        payload = json.loads(row[0])
        if payload["version"] != 2:
            return None
        rows = decode(payload["rows"])
        if not is_list(rows) or not all(is_mapping(r) for r in rows):
            raise ValueError("Invalid public cache rows")
        return (
            cast(list[dict[str, object]], rows),
            datetime.fromisoformat(payload["retrieved_at"]),
            max(0, expiry - time.time()),
        )

    def put(
        self, key: str, rows: list[dict[str, object]], retrieved_at: datetime, ttl: int
    ) -> None:
        payload = json.dumps(
            {
                "version": 2,
                "rows": encode(rows),
                "retrieved_at": retrieved_at.isoformat(),
            },
            allow_nan=False,
            separators=(",", ":"),
        )
        if len(payload.encode()) > 8_000_000:
            return
        now = time.time()
        with closing(self.connection()) as db, db:
            db.execute(
                "INSERT INTO public_cache VALUES (?,?,?,?,?) ON CONFLICT(namespace,key) DO UPDATE SET value=excluded.value, expires=excluded.expires, updated=excluded.updated",
                (
                    self.namespace,
                    sha256(key.encode()).hexdigest(),
                    payload,
                    now + ttl,
                    now,
                ),
            )
            db.execute("DELETE FROM public_cache WHERE expires<=?", (now,))
            db.execute(
                "DELETE FROM public_cache WHERE namespace=? AND key IN (SELECT key FROM public_cache WHERE namespace=? ORDER BY updated DESC LIMIT -1 OFFSET ?)",
                (self.namespace, self.namespace, self.limit),
            )
