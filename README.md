# nospeaky.ai

Captions for videos that shipped without CC.

## Stack (v0.1)

- Static site on **GitHub Pages** → `nospeaky.ai`
- Local engine (this Mac): FastAPI + mlx-whisper + ffmpeg (+ yt-dlp for URLs)
- Engine binds **127.0.0.1:8788 only** — not public yet

## Run locally (test for real)

Terminal A — engine:

```bash
cd "~/workspace/PiStudios/Web Development/nospeaky"
./engine/run.sh
```

Terminal B — site (must be http:// not the live https site — browsers block https→http):

```bash
cd "~/workspace/PiStudios/Web Development/nospeaky"
python3 -m http.server 8765 --bind 127.0.0.1
```

Open: http://127.0.0.1:8765/watch.html  
Drop a file or paste a URL → pick languages → Start → wait for Ready → play / download `.srt`

First run downloads the Whisper model (can take a few minutes).

## Deploy workflow (site)

1. `git pull`
2. Tag backup before risky changes
3. Branch → PR → merge to `main`
4. Never force-push `main`

## DNS

Already pointed at GitHub Pages. HTTPS on.

## Next

- Put engine online behind login + limits (`api.nospeaky.ai`)
- Better models / chunked live cues while playing
- Stripe free tier + Pro
