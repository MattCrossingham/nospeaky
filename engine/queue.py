"""Job queue. Redis if REDIS_URL works, else a locked file on the data volume.

Same commands either way: enqueue, claim, position, qlen.
Later workers = more processes calling claim() against the same Redis.
"""
from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path

DATA = Path(os.environ.get("NOSPEAKY_DATA", Path(__file__).resolve().parent / "data"))
FILE = DATA / "queue.json"
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0").strip()


def _parse_redis(url: str) -> tuple[str, int, int]:
    u = url.replace("redis://", "")
    db = 0
    if "/" in u:
        hostport, db_s = u.split("/", 1)
        try:
            db = int(db_s or 0)
        except ValueError:
            db = 0
    else:
        hostport = u
    if ":" in hostport:
        host, port_s = hostport.rsplit(":", 1)
        port = int(port_s)
    else:
        host, port = hostport, 6379
    return host, port, db


class _Redis:
    def __init__(self, url: str) -> None:
        self.host, self.port, self.db = _parse_redis(url)

    def _talk(self, *parts: str, timeout: float = 3.0) -> bytes:
        buf = f"*{len(parts)}\r\n".encode()
        for p in parts:
            b = p.encode()
            buf += f"${len(b)}\r\n".encode() + b + b"\r\n"
        s = socket.create_connection((self.host, self.port), timeout=timeout)
        try:
            s.sendall(buf)
            s.settimeout(min(timeout, 1.2))
            chunks = b""
            while True:
                try:
                    bit = s.recv(8192)
                except socket.timeout:
                    break
                if not bit:
                    break
                chunks += bit
        finally:
            s.close()
        return chunks

    def ping(self) -> bool:
        try:
            return self._talk("PING").startswith(b"+PONG")
        except OSError:
            return False

    def lpush(self, key: str, val: str) -> None:
        self._talk("LPUSH", key, val)

    def rpop(self, key: str) -> str | None:
        raw = self._talk("RPOP", key)
        if raw.startswith(b"$-1"):
            return None
        if raw.startswith(b"$"):
            _, rest = raw.split(b"\r\n", 1)
            return rest.split(b"\r\n", 1)[0].decode()
        return None

    def lrange(self, key: str) -> list[str]:
        raw = self._talk("LRANGE", key, "0", "-1")
        if not raw.startswith(b"*"):
            return []
        lines = raw.split(b"\r\n")
        out: list[str] = []
        i = 1
        while i < len(lines):
            if lines[i].startswith(b"$") and i + 1 < len(lines):
                out.append(lines[i + 1].decode())
                i += 2
            else:
                i += 1
        return out

    def llen(self, key: str) -> int:
        raw = self._talk("LLEN", key)
        if raw.startswith(b":"):
            return int(raw[1:].split(b"\r\n", 1)[0])
        return 0

    def lrem(self, key: str, val: str) -> None:
        self._talk("LREM", key, "0", val)


class _File:
    def __init__(self) -> None:
        DATA.mkdir(parents=True, exist_ok=True)
        if not FILE.exists():
            FILE.write_text(json.dumps({"free": [], "pro": []}), encoding="utf-8")

    def _load(self) -> dict:
        try:
            return json.loads(FILE.read_text(encoding="utf-8"))
        except Exception:
            return {"free": [], "pro": []}

    def _save(self, d: dict) -> None:
        tmp = FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(d), encoding="utf-8")
        tmp.replace(FILE)

    def lpush(self, key: str, val: str) -> None:
        d = self._load()
        lane = "pro" if key.endswith("pro") else "free"
        lst = d.setdefault(lane, [])
        lst.insert(0, val)
        self._save(d)

    def rpop(self, key: str) -> str | None:
        d = self._load()
        lane = "pro" if key.endswith("pro") else "free"
        lst = d.setdefault(lane, [])
        if not lst:
            return None
        val = lst.pop()
        self._save(d)
        return val

    def lrange(self, key: str) -> list[str]:
        d = self._load()
        lane = "pro" if key.endswith("pro") else "free"
        return list(d.get(lane) or [])

    def llen(self, key: str) -> int:
        return len(self.lrange(key))

    def lrem(self, key: str, val: str) -> None:
        d = self._load()
        lane = "pro" if key.endswith("pro") else "free"
        d[lane] = [x for x in (d.get(lane) or []) if x != val]
        self._save(d)


_backend: _Redis | _File | None = None
_kind = "file"


def backend() -> _Redis | _File:
    global _backend, _kind
    if _backend is not None:
        return _backend
    r = _Redis(REDIS_URL)
    if r.ping():
        _backend = r
        _kind = "redis"
        return _backend
    _backend = _File()
    _kind = "file"
    return _backend


def kind() -> str:
    backend()
    return _kind


def key_for(tier: str) -> str:
    return "nospeaky:q:pro" if (tier or "free") == "pro" else "nospeaky:q:free"


def enqueue(job_id: str, tier: str = "free") -> None:
    backend().lpush(key_for(tier), job_id)


def claim(tier: str = "free") -> str | None:
    return backend().rpop(key_for(tier))


def qlen(tier: str = "free") -> int:
    return backend().llen(key_for(tier))


def position(job_id: str, tier: str = "free") -> int:
    """1 = next to run. 0 = not in queue (running or done)."""
    ids = backend().lrange(key_for(tier))
    # list is LPUSH so index 0 is newest; RPOP takes the tail = oldest = next
    try:
        # oldest is last element
        order = list(reversed(ids))
        return order.index(job_id) + 1
    except ValueError:
        return 0


def drop(job_id: str, tier: str = "free") -> None:
    backend().lrem(key_for(tier), job_id)


def snapshot(tier: str = "free") -> dict:
    n = qlen(tier)
    return {"backend": kind(), "tier": tier, "queued": n}
