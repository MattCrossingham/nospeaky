# nospeaky.ai

Paste a video URL. Pick a language. Captions while it plays.

Live: https://nospeaky.ai · https://nospeaky.ai/watch.html

## Stack

- Static site on **GitHub Pages** → `nospeaky.ai`
- Watch talks to `https://api.nospeaky.ai` (page key is a public gate only — it cannot spend Scribe or Langbly)
- Engine on a **DigitalOcean Sydney** droplet, Docker `nospeaky-engine-1` bound to **127.0.0.1:8788 only**
- **nginx** terminates TLS for `api.nospeaky.ai` and proxies HTTP plus the WebSocket upgrade
- Not the home network. Not pi-node.

## Live path

Default Watch path is streaming captions:

- `wss://api.nospeaky.ai/v1/stream` (chunked faster-whisper, overlay follows the playhead)
- Whole-file `POST /v1/jobs` is fallback if the socket fails

Health: `https://api.nospeaky.ai/health`

## Free tier

- yt-dlp audio-only fetch + **faster-whisper tiny** on the box
- **Langbly** rewrites caption lines for Read-it-in (Argos if Langbly returns nothing)
- 10-minute cap
- 8 jobs and 8 new live clips per hour per IP (SQLite on the data volume)
- One live stream at a time on this box (`STREAM_MAX=1`)

## Pro

Scribe is wired (`pro_ready`) but **locked**. Public Watch has no Pro button. Server token only (`?pro=`). Stripe Checkout is test-mode on Watch (Skip the queue). Live cards need live keys. Homepage still says Later.

## Site deploy

1. `git pull`
2. Tag backup before risky changes
3. Branch → PR → merge to `main`
4. Never force-push `main`

Pages source is `main` `/`. DNS already on GitHub Pages. HTTPS on.

## Local lab (optional)

Engine still runs on a Mac for experiments (`./engine/run.sh` → 127.0.0.1:8788). Serve the site over **http** (`python3 -m http.server 8765 --bind 127.0.0.1`) — browsers block https→http. That path is not production.

## Next

- Worker-pool scaling so different videos can stream at once (this droplet is one live pipeline)
- Stripe billing when Pro goes public
