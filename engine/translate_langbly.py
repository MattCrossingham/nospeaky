"""Caption-line translate via Langbly. Key from env or data volume — never the browser."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterable

from engine.translate_local import iso2

_ENDPOINT = os.environ.get("LANGBLY_ENDPOINT", "https://api.langbly.com/language/translate/v2").strip()
_KEY_FILE = Path("/app/engine/data/langbly_key")


def _key() -> str:
    env = os.environ.get("LANGBLY_API_KEY", "").strip()
    if env:
        return env
    if _KEY_FILE.exists():
        return _KEY_FILE.read_text(encoding="utf-8").strip()
    return ""


def ready() -> bool:
    return bool(_key())


def translate_lines(texts: Iterable[str], target: str, source: str | None = None) -> list[str] | None:
    lines = [t or "" for t in texts]
    if not lines:
        return []
    key = _key()
    if not key:
        return None
    dst = iso2(target) or (target or "").strip().lower()
    src = iso2(source)
    if not dst:
        return None
    if src and src == dst:
        return list(lines)
    payload: dict = {"q": lines, "target": dst, "format": "text"}
    if src and src not in ("auto",):
        payload["source"] = src
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        _ENDPOINT,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-API-Key": key,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=18) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError):
        return None
    rows = ((raw or {}).get("data") or {}).get("translations") or []
    out: list[str] = []
    for i, line in enumerate(lines):
        got = ""
        if i < len(rows) and isinstance(rows[i], dict):
            got = str(rows[i].get("translatedText") or "").strip()
        out.append(got or line)
    if len(out) != len(lines):
        return None
    return out
