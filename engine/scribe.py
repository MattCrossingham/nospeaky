"""Pro captions via ElevenLabs Scribe. Key stays in server env, never the browser."""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

SCRIBE_URL = "https://api.elevenlabs.io/v1/speech-to-text"

SOURCE_ISO = {
    "auto": None,
    "english": "en",
    "chinese": "zh",
    "japanese": "ja",
    "korean": "ko",
    "spanish": "es",
    "french": "fr",
    "german": "de",
    "portuguese": "pt",
    "hindi": "hi",
    "arabic": "ar",
    "en": "en",
    "zh": "zh",
    "ja": "ja",
    "ko": "ko",
    "es": "es",
    "fr": "fr",
    "de": "de",
    "pt": "pt",
    "hi": "hi",
    "ar": "ar",
}


def iso_lang(source_lang: str | None) -> str | None:
    if not source_lang:
        return None
    key = source_lang.strip().lower()
    if key in SOURCE_ISO:
        return SOURCE_ISO[key]
    if len(key) in (2, 3):
        return key
    return None


def words_to_cues(
    words: list[dict[str, Any]],
    max_chars: int = 42,
    max_sec: float = 4.0,
) -> list[dict[str, Any]]:
    cues: list[dict[str, Any]] = []
    cur: list[dict[str, Any]] = []

    def flush() -> None:
        if not cur:
            return
        text = " ".join(x["t"] for x in cur).strip()
        if text:
            cues.append(
                {
                    "start": float(cur[0]["s"]),
                    "end": float(cur[-1]["e"]),
                    "text": text,
                }
            )
        cur.clear()

    for w in words or []:
        if (w.get("type") or "word") not in ("word",):
            continue
        text = (w.get("text") or "").strip()
        if not text:
            continue
        start = float(w.get("start") or 0.0)
        end = float(w.get("end") or start)
        if not cur:
            cur.append({"t": text, "s": start, "e": end})
            continue
        span = end - cur[0]["s"]
        chars = sum(len(x["t"]) for x in cur) + 1 + len(text)
        prev_punct = cur[-1]["t"].endswith((".", "?", "!", "…"))
        if prev_punct or span > max_sec or chars > max_chars:
            flush()
            cur.append({"t": text, "s": start, "e": end})
        else:
            cur.append({"t": text, "s": start, "e": end})
    flush()
    return cues


def transcribe_file(
    path: Path,
    api_key: str,
    model_id: str = "scribe_v2",
    language_code: str | None = None,
    timeout: int = 600,
) -> dict[str, Any]:
    if not api_key:
        raise RuntimeError("Pro is not connected yet.")
    boundary = "----NoSpeaky" + uuid.uuid4().hex
    fields: list[tuple[str, str]] = [
        ("model_id", model_id),
        ("timestamps_granularity", "word"),
        ("tag_audio_events", "false"),
    ]
    if language_code:
        fields.append(("language_code", language_code))

    chunks: list[bytes] = []
    for name, value in fields:
        chunks.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")
        )
    payload = path.read_bytes()
    chunks.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8")
        + payload
        + b"\r\n"
    )
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(chunks)
    req = Request(
        SCRIBE_URL,
        data=body,
        method="POST",
        headers={
            "xi-api-key": api_key,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
        },
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[-400:]
        raise RuntimeError(f"Pro caption service failed ({e.code}): {detail}") from e
    except URLError as e:
        raise RuntimeError(f"Pro caption service unreachable: {e.reason}") from e
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError("Pro caption service returned junk") from e
    if not isinstance(data, dict):
        raise RuntimeError("Pro caption service returned junk")
    return data
