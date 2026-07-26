#!/usr/bin/env python3
"""NoSpeaky local engine — file/URL → speech → subtitles → SRT.

Run (from repo root):
  .venv/bin/uvicorn engine.server:app --host 127.0.0.1 --port 8788

Only binds localhost by default. Do not expose to the public internet
until auth + rate limits exist.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
JOBS = DATA / "jobs"
DATA.mkdir(parents=True, exist_ok=True)
JOBS.mkdir(parents=True, exist_ok=True)

# Small model = faster tests on M4. Bump to medium/large later for quality.
WHISPER_MODEL = os.environ.get("NOSPEAKY_MODEL", "mlx-community/whisper-small-mlx")
MAX_UPLOAD_MB = int(os.environ.get("NOSPEAKY_MAX_UPLOAD_MB", "200"))
MAX_DURATION_SEC = int(os.environ.get("NOSPEAKY_MAX_DURATION_SEC", "600"))  # 10 min public beta
API_KEY = os.environ.get("NOSPEAKY_API_KEY", "").strip()
# Simple in-memory rate limit: N new jobs per IP per hour
JOB_LIMIT_PER_HOUR = int(os.environ.get("NOSPEAKY_JOB_LIMIT_PER_HOUR", "8"))

app = FastAPI(title="NoSpeaky Engine", version="0.1.1")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://nospeaky.ai",
        "https://www.nospeaky.ai",
        "http://nospeaky.ai",
        "http://www.nospeaky.ai",
        "http://127.0.0.1:8765",
        "http://localhost:8765",
        "http://127.0.0.1:5500",
        "http://localhost:5500",
        "null",  # file:// pages during local open
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

_rate: dict[str, list[float]] = {}

_lock = threading.Lock()
_jobs: dict[str, dict[str, Any]] = {}
_model_lock = threading.Lock()
_whisper_ready = False


def _job_dir(job_id: str) -> Path:
    return JOBS / job_id


def _save_meta(job: dict[str, Any]) -> None:
    path = _job_dir(job["id"]) / "meta.json"
    path.write_text(json.dumps(job, indent=2), encoding="utf-8")


def _set(job_id: str, **fields: Any) -> dict[str, Any]:
    with _lock:
        job = _jobs[job_id]
        job.update(fields)
        job["updated_at"] = time.time()
        snap = dict(job)
    _save_meta(snap)
    return snap


def _public(job: dict[str, Any]) -> dict[str, Any]:
    out = {
        "id": job["id"],
        "status": job["status"],
        "progress": job.get("progress", 0),
        "message": job.get("message", ""),
        "error": job.get("error"),
        "source_lang": job.get("source_lang"),
        "target_lang": job.get("target_lang"),
        "detected_language": job.get("detected_language"),
        "cues": job.get("cues") or [],
        "srt": job.get("srt"),
        "vtt": job.get("vtt"),
        "duration": job.get("duration"),
    }
    if job.get("media_name"):
        out["media_url"] = f"/v1/jobs/{job['id']}/media"
    if job.get("srt"):
        out["srt_url"] = f"/v1/jobs/{job['id']}/srt"
    if job.get("vtt"):
        out["vtt_url"] = f"/v1/jobs/{job['id']}/vtt"
    return out


def _ensure_tools() -> None:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg not found on PATH")
    if not shutil.which("ffprobe"):
        raise RuntimeError("ffprobe not found on PATH")


def _run(cmd: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _is_safe_public_url(url: str) -> bool:
    try:
        p = urlparse(url)
    except Exception:
        return False
    if p.scheme not in ("http", "https"):
        return False
    host = (p.hostname or "").lower()
    if not host or host in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
        return False
    # Block obvious internal / link-local
    if host.endswith(".local") or host.endswith(".internal"):
        return False
    if re.match(r"^10\.", host) or re.match(r"^192\.168\.", host) or re.match(r"^172\.(1[6-9]|2\d|3[0-1])\.", host):
        return False
    if host.startswith("169.254."):
        return False
    return True


def _probe_duration(path: Path) -> float | None:
    r = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        timeout=60,
    )
    if r.returncode != 0:
        return None
    try:
        return float((r.stdout or "").strip())
    except ValueError:
        return None


def _extract_wav(src: Path, dst: Path) -> None:
    r = _run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(src),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(dst),
        ],
        timeout=600,
    )
    if r.returncode != 0 or not dst.exists():
        raise RuntimeError(f"ffmpeg failed: {(r.stderr or r.stdout)[-800:]}")


def _fetch_url(url: str, out_dir: Path) -> Path:
    if not shutil.which("yt-dlp"):
        raise RuntimeError("yt-dlp not found — required for URL jobs")
    if not _is_safe_public_url(url):
        raise RuntimeError("URL not allowed")

    out_tmpl = str(out_dir / "source.%(ext)s")
    r = _run(
        [
            "yt-dlp",
            "--no-playlist",
            "-f",
            "bv*+ba/b",
            "--merge-output-format",
            "mp4",
            "-o",
            out_tmpl,
            "--max-filesize",
            f"{MAX_UPLOAD_MB}M",
            url,
        ],
        timeout=900,
    )
    if r.returncode != 0:
        # retry audio-preferred for stubborn pages
        r = _run(
            [
                "yt-dlp",
                "--no-playlist",
                "-f",
                "bestaudio/best",
                "-o",
                out_tmpl,
                "--max-filesize",
                f"{MAX_UPLOAD_MB}M",
                url,
            ],
            timeout=900,
        )
    if r.returncode != 0:
        raise RuntimeError(f"download failed: {(r.stderr or r.stdout)[-800:]}")

    candidates = sorted(out_dir.glob("source.*"), key=lambda p: p.stat().st_mtime, reverse=True)
    # ignore .part etc
    candidates = [p for p in candidates if p.suffix.lower() not in {".part", ".ytdl", ".json"}]
    if not candidates:
        raise RuntimeError("download produced no file")
    return candidates[0]


def _srt_ts(sec: float) -> str:
    if sec < 0:
        sec = 0
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int(round((sec - int(sec)) * 1000))
    if ms == 1000:
        s += 1
        ms = 0
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _vtt_ts(sec: float) -> str:
    return _srt_ts(sec).replace(",", ".")


def _cues_to_srt(cues: list[dict[str, Any]]) -> str:
    parts = []
    for i, c in enumerate(cues, 1):
        parts.append(
            f"{i}\n{_srt_ts(c['start'])} --> {_srt_ts(c['end'])}\n{c['text']}\n"
        )
    return "\n".join(parts)


def _cues_to_vtt(cues: list[dict[str, Any]]) -> str:
    parts = ["WEBVTT", ""]
    for c in cues:
        parts.append(f"{_vtt_ts(c['start'])} --> {_vtt_ts(c['end'])}\n{c['text']}\n")
    return "\n".join(parts)


def _translate_lines(texts: list[str], target: str) -> list[str]:
    if not texts:
        return []
    # Google translate via deep-translator — fine for local testing.
    from deep_translator import GoogleTranslator

    # deep-translator uses ISO codes mostly matching ours
    lang = target
    if lang == "zh":
        lang = "zh-CN"
    tr = GoogleTranslator(source="auto", target=lang)
    out: list[str] = []
    batch: list[str] = []
    max_chars = 4200

    def flush() -> None:
        nonlocal batch, out
        if not batch:
            return
        joined = "\n".join(batch)
        try:
            translated = tr.translate(joined)
        except Exception:
            # fall back line by line
            for line in batch:
                try:
                    out.append(tr.translate(line))
                except Exception:
                    out.append(line)
            batch = []
            return
        lines = translated.split("\n")
        if len(lines) == len(batch):
            out.extend(lines)
        else:
            # mismatch — translate one by one
            for line in batch:
                try:
                    out.append(tr.translate(line))
                except Exception:
                    out.append(line)
        batch = []

    for t in texts:
        t = t or ""
        if sum(len(x) for x in batch) + len(t) + 1 > max_chars:
            flush()
        batch.append(t)
    flush()
    return out


def _transcribe(wav: Path, source_lang: str, target_lang: str) -> tuple[list[dict[str, Any]], str | None]:
    import mlx_whisper

    # Whisper task=translate always goes to English.
    # For other target languages: transcribe then machine-translate.
    want_en = target_lang == "en"
    # If source is known English and target English, plain transcribe.
    # If target English and source not English → whisper translate is best.
    task = "transcribe"
    language = None if source_lang in ("", "auto", None) else source_lang

    if want_en and (language is None or language != "en"):
        # Prefer native whisper translate → English
        task = "translate"
        # language still helps if known
    elif want_en and language == "en":
        task = "transcribe"
    else:
        task = "transcribe"

    global _whisper_ready
    with _model_lock:
        result = mlx_whisper.transcribe(
            str(wav),
            path_or_hf_repo=WHISPER_MODEL,
            verbose=False,
            word_timestamps=False,
            task=task,
            language=language,
        )
        _whisper_ready = True

    detected = result.get("language")
    segments = result.get("segments") or []
    cues: list[dict[str, Any]] = []
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        cues.append(
            {
                "start": float(seg.get("start") or 0.0),
                "end": float(seg.get("end") or 0.0),
                "text": text,
            }
        )

    # If target is not English and not same as detected/source, translate cues.
    src = language or detected or "auto"
    if target_lang not in ("", None) and target_lang != "en":
        # whisper didn't produce target lang (unless source already was target)
        if src != target_lang:
            texts = [c["text"] for c in cues]
            translated = _translate_lines(texts, target_lang)
            for c, t in zip(cues, translated):
                c["text"] = t
    elif target_lang == "en" and task == "transcribe" and src not in ("en", "auto", None):
        # Should have used translate; if not, translate now
        texts = [c["text"] for c in cues]
        translated = _translate_lines(texts, "en")
        for c, t in zip(cues, translated):
            c["text"] = t

    return cues, detected


def _process_job(job_id: str) -> None:
    try:
        job = _jobs[job_id]
        jdir = _job_dir(job_id)
        _set(job_id, status="working", progress=5, message="Preparing media…")

        media_path = Path(job["media_path"])
        if job.get("source_url") and not media_path.exists():
            _set(job_id, progress=10, message="Downloading…")
            media_path = _fetch_url(job["source_url"], jdir)
            # prefer playable mp4 name
            final = jdir / f"media{media_path.suffix.lower() or '.mp4'}"
            if media_path != final:
                media_path.replace(final)
                media_path = final
            _set(job_id, media_path=str(media_path), media_name=media_path.name)

        if not media_path.exists():
            raise RuntimeError("media missing")

        duration = _probe_duration(media_path)
        if duration and duration > MAX_DURATION_SEC:
            raise RuntimeError(f"Video too long ({int(duration)}s). Max is {MAX_DURATION_SEC}s for now.")
        _set(job_id, duration=duration, progress=25, message="Extracting audio…")

        wav = jdir / "audio.wav"
        _extract_wav(media_path, wav)

        _set(job_id, progress=40, message="Listening / writing subtitles…")
        cues, detected = _transcribe(wav, job.get("source_lang") or "auto", job.get("target_lang") or "en")

        _set(job_id, progress=85, message="Building .srt…", detected_language=detected)
        srt = _cues_to_srt(cues)
        vtt = _cues_to_vtt(cues)
        (jdir / "captions.srt").write_text(srt, encoding="utf-8")
        (jdir / "captions.vtt").write_text(vtt, encoding="utf-8")

        _set(
            job_id,
            status="ready",
            progress=100,
            message="Ready",
            cues=cues,
            srt=srt,
            vtt=vtt,
            error=None,
        )
    except Exception as e:
        _set(
            job_id,
            status="failed",
            progress=100,
            message="Failed",
            error=str(e),
        )


def _start_thread(job_id: str) -> None:
    t = threading.Thread(target=_process_job, args=(job_id,), daemon=True)
    t.start()


def _client_ip(request: Request) -> str:
    xf = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for")
    if xf:
        return xf.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _check_key(x_nospeaky_key: str | None = Header(default=None, alias="X-NoSpeaky-Key")) -> None:
    if not API_KEY:
        return  # open local dev
    if not x_nospeaky_key or x_nospeaky_key.strip() != API_KEY:
        raise HTTPException(401, "Invalid or missing API key")


def _rate_limit(ip: str) -> None:
    now = time.time()
    window = 3600.0
    hits = [t for t in _rate.get(ip, []) if now - t < window]
    if len(hits) >= JOB_LIMIT_PER_HOUR:
        raise HTTPException(429, f"Too many jobs from this IP. Limit {JOB_LIMIT_PER_HOUR}/hour while in beta.")
    hits.append(now)
    _rate[ip] = hits


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "model": WHISPER_MODEL,
        "whisper_ready": _whisper_ready,
        "max_upload_mb": MAX_UPLOAD_MB,
        "max_duration_sec": MAX_DURATION_SEC,
        "auth_required": bool(API_KEY),
        "public_beta": True,
    }


@app.post("/v1/jobs")
async def create_job(
    request: Request,
    file: UploadFile | None = File(None),
    url: str | None = Form(None),
    source_lang: str = Form("auto"),
    target_lang: str = Form("en"),
    _: None = Depends(_check_key),
) -> JSONResponse:
    _ensure_tools()
    _rate_limit(_client_ip(request))
    url = (url or "").strip() or None
    if not file and not url:
        raise HTTPException(400, "Provide a file or url")
    if file and url:
        # Prefer file if both sent
        url = None
    if url and not _is_safe_public_url(url):
        raise HTTPException(400, "URL not allowed")

    job_id = uuid.uuid4().hex[:12]
    jdir = _job_dir(job_id)
    jdir.mkdir(parents=True, exist_ok=True)

    media_path = None
    media_name = None
    if file is not None and file.filename:
        raw = await file.read()
        if len(raw) > MAX_UPLOAD_MB * 1024 * 1024:
            raise HTTPException(400, f"File too large (max {MAX_UPLOAD_MB}MB)")
        suffix = Path(file.filename).suffix.lower() or ".mp4"
        if suffix not in {".mp4", ".mov", ".webm", ".mkv", ".m4a", ".mp3", ".wav", ".aac", ".ogg"}:
            # still accept, ffmpeg may handle
            pass
        media_path = jdir / f"media{suffix}"
        media_path.write_bytes(raw)
        media_name = media_path.name

    job = {
        "id": job_id,
        "status": "queued",
        "progress": 1,
        "message": "Queued",
        "error": None,
        "source_lang": source_lang or "auto",
        "target_lang": target_lang or "en",
        "source_url": url,
        "media_path": str(media_path) if media_path else str(jdir / "media.mp4"),
        "media_name": media_name,
        "cues": [],
        "srt": None,
        "vtt": None,
        "detected_language": None,
        "duration": None,
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    with _lock:
        _jobs[job_id] = job
    _save_meta(job)
    _start_thread(job_id)
    return JSONResponse(_public(job))


@app.get("/v1/jobs/{job_id}")
def get_job(job_id: str, _: None = Depends(_check_key)) -> dict[str, Any]:
    with _lock:
        job = _jobs.get(job_id)
    if not job:
        meta = _job_dir(job_id) / "meta.json"
        if meta.exists():
            job = json.loads(meta.read_text(encoding="utf-8"))
            with _lock:
                _jobs[job_id] = job
        else:
            raise HTTPException(404, "job not found")
    return _public(job)


@app.get("/v1/jobs/{job_id}/srt")
def get_srt(job_id: str, _: None = Depends(_check_key)) -> FileResponse:
    path = _job_dir(job_id) / "captions.srt"
    if not path.exists():
        raise HTTPException(404, "srt not ready")
    return FileResponse(path, media_type="application/x-subrip", filename="nospeaky.srt")


@app.get("/v1/jobs/{job_id}/vtt")
def get_vtt(job_id: str, _: None = Depends(_check_key)) -> FileResponse:
    path = _job_dir(job_id) / "captions.vtt"
    if not path.exists():
        raise HTTPException(404, "vtt not ready")
    return FileResponse(path, media_type="text/vtt", filename="nospeaky.vtt")


@app.get("/v1/jobs/{job_id}/media")
def get_media(job_id: str, _: None = Depends(_check_key)) -> FileResponse:
    with _lock:
        job = _jobs.get(job_id)
    if not job:
        meta = _job_dir(job_id) / "meta.json"
        if meta.exists():
            job = json.loads(meta.read_text(encoding="utf-8"))
        else:
            raise HTTPException(404, "job not found")
    path = Path(job.get("media_path") or "")
    if not path.exists():
        raise HTTPException(404, "media not found")
    return FileResponse(path, filename=path.name)


@app.on_event("startup")
def _startup() -> None:
    _ensure_tools()
    # load leftover job metas (read-only resume of status, not auto-rerun)
    for meta in JOBS.glob("*/meta.json"):
        try:
            job = json.loads(meta.read_text(encoding="utf-8"))
            jid = job.get("id") or meta.parent.name
            if job.get("status") in ("queued", "working"):
                job["status"] = "failed"
                job["error"] = "Server restarted during job"
                job["message"] = "Failed"
            with _lock:
                _jobs[jid] = job
        except Exception:
            continue
