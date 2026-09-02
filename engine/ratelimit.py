"""IP rate limits that survive a container restart.

SQLite on the data volume (same mount as jobs). Sliding 1-hour window.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path

DATA = Path(os.environ.get("NOSPEAKY_DATA", Path(__file__).resolve().parent / "data"))
DB = DATA / "rate.sqlite"
WINDOW_SEC = 3600.0
_lock = threading.Lock()
_ready = False


class RateLimited(Exception):
    def __init__(self, kind: str, limit: int) -> None:
        self.kind = kind
        self.limit = limit
        if kind == "stream":
            msg = f"Too many live sessions from this IP. Limit {limit}/hour while in beta."
        else:
            msg = f"Too many jobs from this IP. Limit {limit}/hour while in beta."
        super().__init__(msg)


def _db() -> sqlite3.Connection:
    global _ready
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB), timeout=8, check_same_thread=False)
    if not _ready:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS hits (kind TEXT NOT NULL, ip TEXT NOT NULL, ts REAL NOT NULL)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS hits_kind_ip_ts ON hits(kind, ip, ts)")
        conn.commit()
        _ready = True
    return conn


def hit(kind: str, ip: str, limit: int) -> None:
    """Record one event. Raises RateLimited if this IP is over the hourly cap."""
    if limit <= 0:
        return
    who = (ip or "unknown").strip() or "unknown"
    now = time.time()
    floor = now - WINDOW_SEC
    with _lock:
        conn = _db()
        try:
            conn.execute("DELETE FROM hits WHERE ts < ?", (floor,))
            n = conn.execute(
                "SELECT COUNT(*) FROM hits WHERE kind = ? AND ip = ? AND ts >= ?",
                (kind, who, floor),
            ).fetchone()[0]
            if int(n) >= int(limit):
                raise RateLimited(kind, limit)
            conn.execute("INSERT INTO hits(kind, ip, ts) VALUES (?, ?, ?)", (kind, who, now))
            conn.commit()
        finally:
            conn.close()
