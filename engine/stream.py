"""Chunked streaming captions. One pipeline per (url, language). Fan-out to many clients."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

CHUNK_SEC = int(os.environ.get("NOSPEAKY_STREAM_CHUNK_SEC", "8"))
STREAM_MAX = int(os.environ.get("NOSPEAKY_STREAM_MAX", "1"))


def room_key(url: str, source_lang: str, target_lang: str, tier: str) -> str:
    raw = f"{(url or '').strip()}|{(source_lang or 'auto')}|{(target_lang or 'en')}|{(tier or 'free')}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class Room:
    def __init__(self, key: str, url: str, source_lang: str, target_lang: str, tier: str) -> None:
        self.key = key
        self.url = url
        self.source_lang = source_lang
        self.target_lang = target_lang
        self.tier = tier
        self.lock = threading.Lock()
        self.clients: list[Callable[[dict[str, Any]], None]] = []
        self.cues: list[dict[str, Any]] = []
        self.running = False
        self.done = False
        self.error: str | None = None
        self.started_at = time.time()

    def emit(self, msg: dict[str, Any]) -> None:
        with self.lock:
            dead: list[Callable[[dict[str, Any]], None]] = []
            for send in self.clients:
                try:
                    send(msg)
                except Exception:
                    dead.append(send)
            for d in dead:
                self.clients.remove(d)

    def add_cues(self, cues: list[dict[str, Any]]) -> None:
        with self.lock:
            self.cues.extend(cues)
        for c in cues:
            self.emit({"type": "cue", "start": c["start"], "end": c["end"], "text": c["text"]})


class Hub:
    def __init__(self, transcribe, safe_url) -> None:
        self.transcribe = transcribe
        self.safe_url = safe_url
        self.lock = threading.Lock()
        self.rooms: dict[str, Room] = {}

    def stats(self) -> dict[str, Any]:
        with self.lock:
            rooms = list(self.rooms.values())
        return {
            "rooms": len(rooms),
            "running": sum(1 for r in rooms if r.running and not r.done),
            "clients": sum(len(r.clients) for r in rooms),
            "max": STREAM_MAX,
        }

    def join(self, url: str, source_lang: str, target_lang: str, tier: str, send) -> Room:
        if not url or not self.safe_url(url):
            raise ValueError("URL not allowed")
        key = room_key(url, source_lang, target_lang, tier)
        with self.lock:
            room = self.rooms.get(key)
            running = [r for r in self.rooms.values() if r.running and not r.done]
            if room is None:
                if len(running) >= STREAM_MAX:
                    raise RuntimeError("This box is already streaming another clip. Wait a minute.")
                room = Room(key, url, source_lang, target_lang, tier)
                self.rooms[key] = room
            room.clients.append(send)
            start = not room.running and not room.done
            if start:
                room.running = True
            replay = list(room.cues)
            err = room.error
            done = room.done
        for c in replay:
            send({"type": "cue", "start": c["start"], "end": c["end"], "text": c["text"]})
        if err:
            send({"type": "error", "error": err})
        elif done:
            send({"type": "done", "cues": replay})
        else:
            send({"type": "status", "message": "Streaming captions…", "key": key})
        if start:
            threading.Thread(target=self._run, args=(room,), daemon=True, name=f"stream-{key}").start()
        return room

    def leave(self, room: Room, send) -> None:
        with room.lock:
            if send in room.clients:
                room.clients.remove(send)

    def _run(self, room: Room) -> None:
        tmp = Path(f"/tmp/ns-stream-{room.key}")
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        try:
            self._pipe(room, tmp)
            room.done = True
            room.running = False
            room.emit({"type": "done", "cues": list(room.cues)})
        except Exception as e:
            room.error = str(e)
            room.done = True
            room.running = False
            room.emit({"type": "error", "error": str(e)})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _pipe(self, room: Room, tmp: Path) -> None:
        if not shutil.which("yt-dlp") or not shutil.which("ffmpeg"):
            raise RuntimeError("yt-dlp/ffmpeg missing")
        pattern = str(tmp / "c%05d.wav")
        host = room.url.lower()
        ytdlp = ["yt-dlp", "--js-runtimes", "deno", "--no-playlist"]
        if "youtube.com" in host or "youtu.be" in host:
            ytdlp += ["--extractor-args", "youtube:player_client=android,ios,web"]
        else:
            ytdlp += ["--impersonate", "firefox"]
        ytdlp += ["-f", "bestaudio[abr<=96]/bestaudio/worst", "-o", "-", room.url]
        ff = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", "pipe:0",
            "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
            "-f", "segment", "-segment_time", str(CHUNK_SEC),
            "-reset_timestamps", "1",
            pattern,
        ]
        proc_y = subprocess.Popen(ytdlp, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        proc_f = subprocess.Popen(ff, stdin=proc_y.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if proc_y.stdout:
            proc_y.stdout.close()
        seen: set[str] = set()
        t0 = 0.0
        idle = 0
        while True:
            files = sorted(tmp.glob("c*.wav"))
            progressed = False
            for wav in files:
                name = wav.name
                if name in seen:
                    continue
                # skip the file ffmpeg is still writing (newest while ffmpeg alive)
                if wav == files[-1] and proc_f.poll() is None:
                    continue
                seen.add(name)
                cues, _det = self.transcribe(wav, room.source_lang, room.target_lang)
                shifted = []
                for c in cues:
                    shifted.append({
                        "start": float(c.get("start") or 0) + t0,
                        "end": float(c.get("end") or 0) + t0,
                        "text": c.get("text") or "",
                    })
                try:
                    dur = float(subprocess.check_output(
                        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                         "-of", "default=nw=1:nk=1", str(wav)],
                        text=True, timeout=20,
                    ).strip() or CHUNK_SEC)
                except Exception:
                    dur = float(CHUNK_SEC)
                t0 += max(0.5, dur)
                if shifted:
                    room.add_cues(shifted)
                    room.emit({"type": "status", "message": f"Live · {int(t0)}s captioned"})
                try:
                    wav.unlink()
                except OSError:
                    pass
                progressed = True
            if proc_f.poll() is not None and proc_y.poll() is not None:
                # last file
                for wav in sorted(tmp.glob("c*.wav")):
                    if wav.name in seen:
                        continue
                    seen.add(wav.name)
                    cues, _det = self.transcribe(wav, room.source_lang, room.target_lang)
                    shifted = [{
                        "start": float(c.get("start") or 0) + t0,
                        "end": float(c.get("end") or 0) + t0,
                        "text": c.get("text") or "",
                    } for c in cues]
                    if shifted:
                        room.add_cues(shifted)
                break
            if not progressed:
                idle += 1
                if idle > 180:
                    raise RuntimeError("Stream stalled")
                time.sleep(0.4)
            else:
                idle = 0
        err = (proc_f.stderr.read() if proc_f.stderr else b"")[-400:]
        if proc_f.returncode not in (0, None) and not room.cues:
            raise RuntimeError(err.decode("utf-8", "replace") or "ffmpeg stream failed")
