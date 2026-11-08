import sqlite3
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from trading212_mcp.market.cache import CachedResponse, TTLCache


def test_public_json_cache_survives_restart_without_rounding_prices(tmp_path):
    path = tmp_path / "market.sqlite3"
    times = pd.date_range("2026-01-01", periods=3)
    prices = pd.DataFrame(
        {"Adj Close": [1.2345678901234567, np.nan, 100.00000000000001]}, index=times
    )
    statements = pd.DataFrame({times[-1]: [10.25, 0.0]}, index=["Revenue", "NetIncome"])
    original = CachedResponse(
        [{"value": (prices, statements), "info": {"a": 1, "missing": None}}],
        datetime(2026, 1, 2, tzinfo=timezone.utc),
    )
    TTLCache(3600, 5, path, "yahoo").put("history", original)
    restored = TTLCache(3600, 5, path, "yahoo").get("history")
    assert restored is not None
    assert restored.retrieved_at == original.retrieved_at
    restored_frames = restored.rows[0]["value"]
    assert isinstance(restored_frames, tuple)
    pd.testing.assert_frame_equal(restored_frames[0], prices, check_freq=False)
    pd.testing.assert_frame_equal(restored_frames[1], statements)
    assert restored.rows[0]["info"] == original.rows[0]["info"]
    with sqlite3.connect(path) as db:
        assert "history" not in db.execute("SELECT key FROM public_cache").fetchone()[0]


def test_persistence_respects_expiry_current_ttl_and_source_namespaces(
    tmp_path, monkeypatch
):
    clock = [1000.0]
    monkeypatch.setattr("trading212_mcp.market.disk_cache.time.time", lambda: clock[0])
    path = tmp_path / "cache.db"
    original = CachedResponse([{"price": 100}], datetime.now(timezone.utc))
    TTLCache(3600, 2, path, "yahoo").put("same", original)
    assert TTLCache(3600, 2, path, "fmp").get("same") is None
    clock[0] += 100
    assert TTLCache(60, 2, path, "yahoo").get("same") is None
    assert TTLCache(3600, 2, path, "yahoo").get("same") is not None
    clock[0] += 3501
    assert TTLCache(3600, 2, path, "yahoo").get("same") is None


def test_cache_bound_and_corruption_fail_open_without_fake_financial_data(tmp_path):
    path = tmp_path / "cache.db"
    cache = TTLCache(3600, 1, path)
    for key in ("first", "second"):
        cache.put(key, CachedResponse([{"value": key}], datetime.now(timezone.utc)))
    assert TTLCache(3600, 1, path).get("first") is None
    with sqlite3.connect(path) as db:
        db.execute("UPDATE public_cache SET value='invalid-json'")
    assert TTLCache(3600, 1, path).get("second") is None
    path.write_bytes(b"not a sqlite database")
    cache = TTLCache(3600, 1, path)
    cache.put("memory", CachedResponse([{"value": 10}], datetime.now(timezone.utc)))
    assert cache.get("memory").rows == [{"value": 10}]


def test_disk_cache_is_disabled_by_default_and_zero_ttl_never_persists(tmp_path):
    assert TTLCache(60, 2).disk is None
    path = tmp_path / "disabled.db"
    cache = TTLCache(0, 2, path)
    cache.put("zero", CachedResponse([], datetime.now(timezone.utc)))
    assert not path.exists()
    assert cache.get("zero") is None


def test_yahoo_history_timezone_and_dst_survive_persistent_cache(tmp_path):
    dates = pd.date_range("2026-03-01", periods=30, tz="America/New_York", name="Date")
    frame = pd.DataFrame({"Adj Close": np.arange(1.0, 31.0)}, index=dates)
    path = tmp_path / "tz.sqlite3"
    TTLCache(3600, 2, path).put(
        "dst", CachedResponse([{"value": frame}], datetime.now(timezone.utc))
    )
    restored = TTLCache(3600, 2, path).get("dst")
    pd.testing.assert_frame_equal(restored.rows[0]["value"], frame, check_freq=False)
    assert isinstance(restored.rows[0]["value"].index, pd.DatetimeIndex)
