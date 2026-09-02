#!/usr/bin/env python3
"""NoSpeaky local engine — file/URL → speech → subtitles → SRT.

Run (from repo root):
  .venv/bin/uvicorn engine.server:app --host 127.0.0.1 --port 8788

Only binds localhost by default. Do not expose to the public internet
until auth + rate limits exist.
"""
from __future__ import annotations

import asyncio
import hmac
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

from engine import queue as jobq
from engine.stream import Hub as StreamHub
from engine.scribe import iso_lang, transcribe_file, words_to_cues
from engine.translate_local import iso2 as _iso2
from engine.translate_local import translate_lines as _local_translate

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
JOBS = DATA / "jobs"
DATA.mkdir(parents=True, exist_ok=True)
JOBS.mkdir(parents=True, exist_ok=True)

# Small model = faster tests. Cloud default is faster-whisper small.
WHISPER_MODEL = os.environ.get(
    "NOSPEAKY_MODEL",
    "Systran/faster-whisper-small",
)
WHISPER_BACKEND = os.environ.get("NOSPEAKY_BACKEND", "auto")  # auto|mlx|faster
WHISPER_DEVICE = os.environ.get("NOSPEAKY_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE = os.environ.get("NOSPEAKY_COMPUTE_TYPE", "int8")
MAX_UPLOAD_MB = int(os.environ.get("NOSPEAKY_MAX_UPLOAD_MB", "200"))
MAX_DURATION_SEC = int(os.environ.get("NOSPEAKY_MAX_DURATION_SEC", "600"))  # 10 min public beta
API_KEY = os.environ.get("NOSPEAKY_API_KEY", "").strip()
# Simple in-memory rate limit: N new jobs per IP per hour
JOB_LIMIT_PER_HOUR = int(os.environ.get("NOSPEAKY_JOB_LIMIT_PER_HOUR", "8"))
# Optional: disable yt-dlp URL fetch on public cloud until hardened further
ALLOW_URL_FETCH = os.environ.get("NOSPEAKY_ALLOW_URL_FETCH", "1").strip() not in {"0", "false", "no"}
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "").strip()
ELEVENLABS_STT_MODEL = os.environ.get("ELEVENLABS_STT_MODEL", "scribe_v2").strip() or "scribe_v2"
PRO_MAX_DURATION_SEC = int(os.environ.get("NOSPEAKY_PRO_MAX_DURATION_SEC", "3600"))
PRO_MAX_CONCURRENT = int(os.environ.get("NOSPEAKY_PRO_MAX_CONCURRENT", "4"))


def _pro_gate_token() -> str:
    env = os.environ.get("NOSPEAKY_PRO_TOKEN", "").strip()
    if env:
        return env
    p = Path("/app/engine/data/pro_token")
    if p.exists():
        return p.read_text(encoding="utf-8").strip()
    return ""


def _pro_token_ok(got: str | None) -> bool:
    need = _pro_gate_token()
    if not need:
        return False
    got_b = (got or "").strip().encode("utf-8")
    need_b = need.encode("utf-8")
    if len(got_b) != len(need_b):
        return False
    return hmac.compare_digest(got_b, need_b)

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
_job_q: list[str] = []
_worker_busy = False
_model_lock = threading.Lock()
_whisper_ready = False
_pro_sema = threading.Semaphore(PRO_MAX_CONCURRENT)
_stream_hub: StreamHub | None = None


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
        "duration": job.get("source_duration") or job.get("duration"),
        "eta_sec": _eta_sec(job),
        "heard": job.get("heard"),
        "tier": job.get("tier") or "free",
        "queue_pos": job.get("queue_pos") or 0,
        "queue_len": job.get("queue_len") or 0,
    }
    if job.get("media_name"):
        out["media_url"] = f"/v1/jobs/{job['id']}/media"
    if job.get("srt"):
        out["srt_url"] = f"/v1/jobs/{job['id']}/srt"
    if job.get("vtt"):
        out["vtt_url"] = f"/v1/jobs/{job['id']}/vtt"
    embed = _embed_url(job.get("source_url"))
    if embed:
        out["embed_url"] = embed
    return out


def _eta_sec(job: dict[str, Any]) -> int:
    st = str(job.get("status") or "").lower()
    if st in {"ready", "done", "completed", "failed", "error"}:
        return 0
    pos = int(job.get("queue_pos") or 0)
    if st == "queued" and pos > 1:
        return int(pos * 75)
    now = time.time()
    created = float(job.get("created_at") or now)
    elapsed = max(0.0, now - created)
    src = job.get("source_duration")
    heard = job.get("heard")
    progress = float(job.get("progress") or 0)
    if src:
        remain_audio = float(src)
        if heard is not None:
            remain_audio = max(0.0, float(src) - float(heard))
        return int(max(0.0, remain_audio + 6.0))
    if progress > 8 and elapsed > 3:
        return int(max(0.0, elapsed * (100.0 - progress) / max(progress, 1.0)))
    return int(max(8.0, 90.0 - elapsed))


def _probe_url_duration(url: str) -> float | None:
    if not shutil.which("yt-dlp"):
        return None
    r = _run(
        [
            "yt-dlp",
            "--js-runtimes",
            "deno",
            "--impersonate",
            "firefox",
            "--no-playlist",
            "--print",
            "duration",
            url,
        ],
        timeout=45,
    )
    if r.returncode != 0:
        return None
    lines = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
    if not lines:
        return None
    try:
        val = float(lines[-1])
    except ValueError:
        return None
    if val <= 0:
        return None
    return val


def _embed_url(url: str | None) -> str | None:
    if not url:
        return None
    u = url.strip()
    m = re.search(r"(?:dai\.ly/|dailymotion\.com/video/)([A-Za-z0-9]+)", u, re.I)
    if m:
        return f"https://www.dailymotion.com/embed/video/{m.group(1)}?autoplay=1"
    m = re.search(r"(?:youtube\.com/watch\?v=|youtu\.be/)([\w-]{6,})", u, re.I)
    if m:
        return f"https://www.youtube.com/embed/{m.group(1)}?autoplay=1"
    return None


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


def _is_youtube(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return "youtube.com" in host or host == "youtu.be" or host.endswith(".youtube.com")


def _ytdlp_base(url: str) -> list[str]:
    cmd = ["yt-dlp", "--js-runtimes", "deno", "--no-playlist"]
    if _is_youtube(url):
        cmd += ["--extractor-args", "youtube:player_client=android,ios,web"]
    else:
        cmd += ["--impersonate", "firefox"]
    return cmd


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
    """Audio only. Do not pull full video — we translate while they watch the embed."""
    if not shutil.which("yt-dlp"):
        raise RuntimeError("yt-dlp not found — required for URL jobs")
    if not _is_safe_public_url(url):
        raise RuntimeError("URL not allowed")

    out_tmpl = str(out_dir / "audio.%(ext)s")
    cmd = _ytdlp_base(url) + [
        "-f",
        "bestaudio[abr<=96]/bestaudio/worst",
        "-x",
        "--audio-format",
        "wav",
        "--audio-quality",
        "7",
        "-o",
        out_tmpl,
        "--max-filesize",
        f"{MAX_UPLOAD_MB}M",
        url,
    ]
    r = _run(cmd, timeout=300)
    if r.returncode != 0:
        r = _run(
            _ytdlp_base(url) + [
                "-f",
                "bestaudio/worst",
                "-o",
                str(out_dir / "audio.%(ext)s"),
                "--max-filesize",
                f"{MAX_UPLOAD_MB}M",
                url,
            ],
            timeout=300,
        )
    if r.returncode != 0:
        raise RuntimeError(f"download failed: {(r.stderr or r.stdout)[-800:]}")

    candidates = sorted(out_dir.glob("audio.*"), key=lambda p: p.stat().st_mtime, reverse=True)
    candidates = [p for p in candidates if p.suffix.lower() not in {".part", ".ytdl", ".json"}]
    if not candidates:
        raise RuntimeError("download produced no file")
    return candidates[0]


def _fetch_playable_video(url: str, out_dir: Path) -> Path:
    """Small picture for the HTML5 player. Never 1080p."""
    if not shutil.which("yt-dlp"):
        raise RuntimeError("yt-dlp not found — required for URL jobs")
    if not _is_safe_public_url(url):
        raise RuntimeError("URL not allowed")
    out = out_dir / "play.mp4"
    r = _run(
        _ytdlp_base(url) + [
            "-f",
            "best[height<=360][ext=mp4]/best[height<=360]/worst[ext=mp4]/worst",
            "--merge-output-format",
            "mp4",
            "-o",
            str(out),
            "--max-filesize",
            "80M",
            url,
        ],
        timeout=300,
    )
    if r.returncode != 0 or not out.exists() or out.stat().st_size < 1000:
        raise RuntimeError(f"playable video failed: {(r.stderr or r.stdout)[-400:]}")
    return out


def _try_attach_playable(job_id: str, url: str, jdir: Path) -> None:
    try:
        _set(job_id, progress=92, message="Getting the video…")
        vid = _fetch_playable_video(url, jdir)
        _set(job_id, media_path=str(vid), media_name=vid.name)
    except Exception:
        return


def _audio_stream_url(url: str) -> str:
    if not shutil.which("yt-dlp"):
        raise RuntimeError("yt-dlp not found — required for URL jobs")
    r = _run(
        _ytdlp_base(url) + [
            "-f",
            "bestaudio[abr<=96]/bestaudio/worst",
            "-g",
            url,
        ],
        timeout=60,
    )
    if r.returncode != 0:
        raise RuntimeError(f"download failed: {(r.stderr or r.stdout)[-800:]}")
    lines = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
    if not lines:
        raise RuntimeError("no audio stream")
    return lines[-1]


def _process_url_live(job_id: str, url: str, jdir: Path, source_lang: str, target_lang: str) -> None:
    """Chunked audio: captions as soon as each few seconds are heard."""
    _set(job_id, status="working", progress=4, message="Timing the clip…")
    src_dur = _probe_url_duration(url)
    _set(
        job_id,
        status="working",
        progress=8,
        message="Translating…",
        source_duration=src_dur,
        duration=src_dur,
        heard=0,
    )
    stream = _audio_stream_url(url)
    chunk_dir = jdir / "chunks"
    chunk_dir.mkdir(parents=True, exist_ok=True)
    out_pat = str(chunk_dir / "c_%03d.wav")
    proc = subprocess.Popen(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            stream,
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            "-f",
            "segment",
            "-segment_time",
            "5",
            "-reset_timestamps",
            "1",
            out_pat,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    cues: list[dict[str, Any]] = []
    offset = 0.0
    seen: set[Path] = set()
    t0 = time.time()
    detected: str | None = None

    def consider(files: list[Path], include_last: bool) -> None:
        nonlocal offset, detected
        ready = files if include_last else files[:-1]
        for f in ready:
            if f in seen:
                continue
            if f.stat().st_size < 2000:
                continue
            seen.add(f)
            part, det = _transcribe(f, source_lang, target_lang)
            if det:
                detected = det
            for c in part:
                cues.append(
                    {
                        "start": float(c["start"]) + offset,
                        "end": float(c["end"]) + offset,
                        "text": c["text"],
                    }
                )
            dur = _probe_duration(f) or 5.0
            offset += dur
            _set(
                job_id,
                cues=list(cues),
                detected_language=detected,
                duration=src_dur or offset,
                source_duration=src_dur,
                heard=offset,
                progress=min(90, 12 + len(cues) * 2),
                message="Translating…",
                status="working",
            )
            if offset > MAX_DURATION_SEC:
                proc.kill()
                return

    try:
        while True:
            files = sorted(chunk_dir.glob("c_*.wav"))
            finished = proc.poll() is not None
            consider(files, include_last=finished)
            if finished:
                break
            if time.time() - t0 > 900:
                proc.kill()
                raise RuntimeError("translate timed out")
            if offset > MAX_DURATION_SEC:
                break
            time.sleep(0.2)
    finally:
        if proc.poll() is None:
            proc.kill()

    srt = _cues_to_srt(cues)
    vtt = _cues_to_vtt(cues)
    (jdir / "captions.srt").write_text(srt, encoding="utf-8")
    (jdir / "captions.vtt").write_text(vtt, encoding="utf-8")
    _try_attach_playable(job_id, url, jdir)
    _set(
        job_id,
        status="ready",
        progress=100,
        message="Ready",
        cues=cues,
        srt=srt,
        vtt=vtt,
        detected_language=detected,
        duration=offset,
        source_duration=src_dur or offset,
        heard=offset,
        error=None,
    )


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


def _junk_translation(s: str) -> bool:
    t = (s or "").lower()
    return any(
        x in t
        for x in (
            "error 500",
            "server error",
            "that's an error",
            "there was an error",
            "please try again later",
            "that's all we know",
        )
    )


def _translate_lines(texts: list[str], target: str, source: str | None = None) -> list[str]:
    if not texts:
        return []
    got = _local_translate(texts, target, source)
    if len(got) != len(texts):
        return list(texts)
    out = []
    for src, dst in zip(texts, got):
        if not dst or _junk_translation(dst):
            out.append(src)
        else:
            out.append(dst)
    return out


_faster_model = None


def _pick_backend() -> str:
    if WHISPER_BACKEND in ("mlx", "faster"):
        return WHISPER_BACKEND
    # auto
    try:
        import mlx_whisper  # noqa: F401

        return "mlx"
    except Exception:
        return "faster"


def _get_faster_model():
    global _faster_model
    if _faster_model is None:
        from faster_whisper import WhisperModel

        _faster_model = WhisperModel(
            WHISPER_MODEL,
            device=WHISPER_DEVICE,
            compute_type=WHISPER_COMPUTE_TYPE,
        )
    return _faster_model


def _clean_hallucination(text: str) -> str:
    words = [w for w in (text or "").split() if w]
    if not words:
        return ""
    out: list[str] = []
    prev = ""
    n = 0
    for w in words:
        key = w.lower().strip(".,!?;:\"'")
        if key == prev:
            n += 1
            if n <= 2:
                out.append(w)
        else:
            prev = key
            n = 1
            out.append(w)
    uniq = {w.lower().strip(".,!?;:\"'") for w in out}
    if len(uniq) <= 1 and len(out) >= 4:
        return ""
    if len(out) >= 12 and len(uniq) <= 2:
        return ""
    return " ".join(out)


def _transcribe(wav: Path, source_lang: str, target_lang: str, on_partial=None) -> tuple[list[dict[str, Any]], str | None]:
    # Whisper task=translate always goes to English.
    # For other target languages: transcribe then machine-translate.
    want_en = target_lang == "en"
    language = None if source_lang in ("", "auto", None) else source_lang

    if want_en and (language is None or language != "en"):
        task = "translate"
    else:
        task = "transcribe"

    backend = _pick_backend()
    global _whisper_ready
    cues: list[dict[str, Any]] = []
    detected: str | None = None

    with _model_lock:
        if backend == "mlx":
            import mlx_whisper

            # Mac path expects mlx repo ids by default if user still has old env
            model_id = WHISPER_MODEL
            if model_id.startswith("Systran/") or "faster-whisper" in model_id:
                model_id = "mlx-community/whisper-small-mlx"
            result = mlx_whisper.transcribe(
                str(wav),
                path_or_hf_repo=model_id,
                verbose=False,
                word_timestamps=False,
                task=task,
                language=language,
            )
            _whisper_ready = True
            detected = result.get("language")
            for seg in result.get("segments") or []:
                text = _clean_hallucination((seg.get("text") or "").strip())
                if not text:
                    continue
                cues.append(
                    {
                        "start": float(seg.get("start") or 0.0),
                        "end": float(seg.get("end") or 0.0),
                        "text": text,
                    }
                )
        else:
            model = _get_faster_model()
            segments, info = model.transcribe(
                str(wav),
                task=task,
                language=language,
                vad_filter=True,
                condition_on_previous_text=False,
                compression_ratio_threshold=2.2,
                no_speech_threshold=0.55,
            )
            _whisper_ready = True
            detected = getattr(info, "language", None)
            for seg in segments:
                text = _clean_hallucination((seg.text or "").strip())
                if not text:
                    continue
                cues.append(
                    {
                        "start": float(seg.start or 0.0),
                        "end": float(seg.end or 0.0),
                        "text": text,
                    }
                )
                if on_partial:
                    on_partial(list(cues), detected)

    # If target is not English and not same as detected/source, translate cues.
    src = language or detected or "auto"
    if target_lang not in ("", None) and target_lang != "en":
        if src != target_lang:
            texts = [c["text"] for c in cues]
            translated = _translate_lines(texts, target_lang)
            for c, t in zip(cues, translated):
                c["text"] = t
    elif target_lang == "en" and task == "transcribe" and src not in ("en", "auto", None):
        texts = [c["text"] for c in cues]
        translated = _translate_lines(texts, "en")
        for c, t in zip(cues, translated):
            c["text"] = t

    return cues, detected


def _process_pro(job_id: str) -> None:
    job = _jobs[job_id]
    jdir = _job_dir(job_id)
    source_lang = job.get("source_lang") or "auto"
    target_lang = job.get("target_lang") or "en"
    _set(job_id, status="working", progress=6, message="Timing the clip…")

    media_path: Path | None = None
    if job.get("source_url"):
        src_dur = _probe_url_duration(job["source_url"])
        cap = PRO_MAX_DURATION_SEC
        if src_dur and src_dur > cap:
            raise RuntimeError(f"Video too long ({int(src_dur)}s). Pro max is {cap}s for now.")
        _set(
            job_id,
            progress=12,
            message="Fetching audio…",
            source_duration=src_dur,
            duration=src_dur,
            heard=0,
        )
        media_path = _fetch_url(job["source_url"], jdir)
    else:
        media_path = Path(job["media_path"])
        if not media_path.exists():
            raise RuntimeError("media missing")
        src_dur = _probe_duration(media_path)
        cap = PRO_MAX_DURATION_SEC
        if src_dur and src_dur > cap:
            raise RuntimeError(f"Video too long ({int(src_dur)}s). Pro max is {cap}s for now.")
        _set(job_id, source_duration=src_dur, duration=src_dur, heard=0, progress=12)

    wav = jdir / "pro.wav"
    if media_path.suffix.lower() == ".wav":
        wav = media_path
    else:
        _extract_wav(media_path, wav)

    _set(job_id, progress=35, message="Translating…")
    data = transcribe_file(
        wav,
        ELEVENLABS_API_KEY,
        model_id=ELEVENLABS_STT_MODEL,
        language_code=iso_lang(source_lang),
    )
    detected = data.get("language_code")
    cues = words_to_cues(data.get("words") or [])
    if not cues:
        text = (data.get("text") or "").strip()
        if text:
            cues = [{"start": 0.0, "end": float(src_dur or 4.0), "text": text}]

    src = _iso2(iso_lang(source_lang) or detected) or iso_lang(source_lang) or detected
    tgt = target_lang or "en"
    if tgt not in ("", None, "same") and _iso2(tgt) != _iso2(src) and _iso2(tgt) != src:
        _set(job_id, progress=70, message="Translating the lines…")
        texts = [c["text"] for c in cues]
        translated = _translate_lines(texts, tgt, source=src)
        for c, t in zip(cues, translated):
            c["text"] = t

    _set(job_id, progress=85, message="Building .srt…", detected_language=detected, cues=list(cues))
    srt = _cues_to_srt(cues)
    vtt = _cues_to_vtt(cues)
    (jdir / "captions.srt").write_text(srt, encoding="utf-8")
    (jdir / "captions.vtt").write_text(vtt, encoding="utf-8")
    if job.get("source_url"):
        _try_attach_playable(job_id, job["source_url"], jdir)
    _set(
        job_id,
        status="ready",
        progress=100,
        message="Ready",
        cues=cues,
        srt=srt,
        vtt=vtt,
        detected_language=detected,
        duration=src_dur,
        source_duration=src_dur,
        heard=src_dur,
        error=None,
    )


def _process_job(job_id: str) -> None:
    try:
        job = _jobs[job_id]
        if (job.get("tier") or "free") == "pro":
            _process_pro(job_id)
            return
        jdir = _job_dir(job_id)
        if job.get("source_url"):
            _process_url_live(
                job_id,
                job["source_url"],
                jdir,
                job.get("source_lang") or "auto",
                job.get("target_lang") or "en",
            )
            return

        _set(job_id, status="working", progress=5, message="Preparing media…")

        media_path = Path(job["media_path"])
        if not media_path.exists():
            raise RuntimeError("media missing")

        duration = _probe_duration(media_path)
        if duration and duration > MAX_DURATION_SEC:
            raise RuntimeError(f"Video too long ({int(duration)}s). Max is {MAX_DURATION_SEC}s for now.")
        _set(job_id, duration=duration, source_duration=duration, heard=0, progress=20, message="Translating…")

        wav = jdir / "audio.wav"
        if media_path.suffix.lower() == ".wav":
            wav = media_path
        else:
            _extract_wav(media_path, wav)

        def _partial(cues_so_far, detected=None):
            _set(
                job_id,
                cues=cues_so_far,
                detected_language=detected,
                progress=min(85, 25 + len(cues_so_far) * 2),
                message="Translating…",
                status="working",
            )

        cues, detected = _transcribe(
            wav,
            job.get("source_lang") or "auto",
            job.get("target_lang") or "en",
            on_partial=_partial,
        )

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


def _stamp_queue(job_id: str) -> None:
    job = _jobs.get(job_id) or {}
    tier = job.get("tier") or "free"
    pos = jobq.position(job_id, tier)
    n = jobq.qlen(tier)
    if job:
        job["queue_pos"] = pos
        job["queue_len"] = n
        if pos > 1:
            job["status"] = "queued"
            job["message"] = f"Number {pos} in the queue"


def _start_thread(job_id: str) -> None:
    job = _jobs.get(job_id) or {}
    tier = job.get("tier") or "free"
    jobq.enqueue(job_id, tier)
    _stamp_queue(job_id)
    _kick_worker(tier)


def _kick_worker(tier: str = "free") -> None:
    global _worker_busy
    if (tier or "free") == "pro":
        job_id = jobq.claim("pro")
        if not job_id:
            return

        def run_pro() -> None:
            _set(job_id, status="queued", message="Waiting for a Pro slot…", queue_pos=0)
            _pro_sema.acquire()
            try:
                _process_job(job_id)
            finally:
                _pro_sema.release()
                _kick_worker("pro")

        threading.Thread(target=run_pro, daemon=True).start()
        return

    with _lock:
        if _worker_busy:
            return
        job_id = jobq.claim("free")
        if not job_id:
            return
        _worker_busy = True

    def run() -> None:
        global _worker_busy
        try:
            _set(job_id, status="working", message="Translating…", queue_pos=0)
            _process_job(job_id)
        finally:
            with _lock:
                _worker_busy = False
            _kick_worker("free")

    threading.Thread(target=run, daemon=True).start()


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
        "backend": _pick_backend(),
        "model": WHISPER_MODEL,
        "whisper_ready": _whisper_ready,
        "max_upload_mb": MAX_UPLOAD_MB,
        "max_duration_sec": MAX_DURATION_SEC,
        "auth_required": bool(API_KEY),
        "allow_url_fetch": ALLOW_URL_FETCH,
        "public_beta": True,
        "home_network": False,
        "pro_ready": bool(ELEVENLABS_API_KEY),
        "pro_max_duration_sec": PRO_MAX_DURATION_SEC,
        "pro_locked": True,
        "queue": jobq.snapshot("free"),
        "stream": (_stream_hub.stats() if _stream_hub else {"rooms": 0, "running": 0, "clients": 0, "max": 1}),
    }


@app.post("/v1/jobs")
async def create_job(
    request: Request,
    file: UploadFile | None = File(None),
    url: str | None = Form(None),
    source_lang: str = Form("auto"),
    target_lang: str = Form("en"),
    tier: str = Form("free"),
    pro_token: str = Form(""),
    _: None = Depends(_check_key),
) -> JSONResponse:
    _ensure_tools()
    _rate_limit(_client_ip(request))
    url = (url or "").strip() or None
    tier_n = (tier or "free").strip().lower()
    if tier_n not in ("free", "pro"):
        raise HTTPException(400, "tier must be free or pro")
    if tier_n == "pro" and not ELEVENLABS_API_KEY:
        raise HTTPException(503, "Pro is not connected yet.")
    if tier_n == "pro" and not _pro_token_ok(pro_token):
        raise HTTPException(403, "Pro is locked.")
    if not file and not url:
        raise HTTPException(400, "Provide a file or url")
    if file and url:
        # Prefer file if both sent
        url = None
    if url and not ALLOW_URL_FETCH:
        raise HTTPException(400, "URL fetch disabled on this server. Upload a file instead.")
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
        "tier": tier_n,
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
    if str(job.get("status") or "") == "queued":
        _stamp_queue(job["id"])
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
            st = job.get("status")
            if st == "working":
                job["status"] = "failed"
                job["error"] = "Server restarted during job"
                job["message"] = "Failed"
            with _lock:
                _jobs[jid] = job
            if st == "queued":
                _start_thread(jid)
        except Exception:
            continue


def _get_stream_hub() -> StreamHub:
    global _stream_hub
    if _stream_hub is None:
        _stream_hub = StreamHub(_transcribe, _is_safe_public_url)
    return _stream_hub


@app.websocket("/v1/stream")
async def stream_captions(ws: WebSocket) -> None:
    await ws.accept()
    loop = asyncio.get_running_loop()

    def send(msg: dict[str, Any]) -> None:
        asyncio.run_coroutine_threadsafe(ws.send_json(msg), loop)

    try:
        start = await asyncio.wait_for(ws.receive_json(), timeout=20)
    except Exception:
        try:
            await ws.close()
        except Exception:
            pass
        return

    got = str(start.get("key") or "").encode()
    need = API_KEY.encode() if API_KEY else b""
    if need and not (len(got) == len(need) and hmac.compare_digest(got, need)):
        await ws.send_json({"type": "error", "error": "bad key"})
        await ws.close()
        return

    url = (start.get("url") or "").strip()
    source_lang = start.get("source_lang") or "auto"
    target_lang = start.get("target_lang") or "en"
    tier = (start.get("tier") or "free").strip().lower()
    if tier == "pro" and not _pro_token_ok(start.get("pro_token")):
        await ws.send_json({"type": "error", "error": "Pro is locked."})
        await ws.close()
        return
    if not url:
        await ws.send_json({"type": "error", "error": "Provide a url"})
        await ws.close()
        return

    room = None
    try:
        room = _get_stream_hub().join(url, source_lang, target_lang, tier, send)
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await ws.send_json({"type": "error", "error": str(e)})
        except Exception:
            pass
    finally:
        if room is not None:
            _get_stream_hub().leave(room, send)
