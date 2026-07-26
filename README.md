# nospeaky.ai

Captions for videos that shipped without CC.

## Stack (v0)

- Static site on **GitHub Pages**
- Custom domain: `nospeaky.ai`
- Transcription engine: not wired yet (`ENGINE_LIVE = false` in `js/app.js`)

## Local

Open `index.html` or any static server:

```bash
cd "~/workspace/PiStudios/Web Development/nospeaky"
python3 -m http.server 8765
```

## Deploy workflow

Same discipline as pistudios.app:

1. `git pull`
2. Tag backup: `git tag backup-$(date +%Y%m%d-%H%M%S) && git push origin --tags`
3. Branch → PR → merge to `main` (no direct main push when protection is on)
4. Never force-push `main`

## DNS (GitHub Pages)

At the registrar for `nospeaky.ai`:

- **A** records for apex → GitHub Pages IPs:
  - `185.199.108.153`
  - `185.199.109.153`
  - `185.199.110.153`
  - `185.199.111.153`
- optional **AAAA**:
  - `2606:50c0:8000::153`
  - `2606:50c0:8001::153`
  - `2606:50c0:8002::153`
  - `2606:50c0:8003::153`
- **CNAME** `www` → `mattcrossingham.github.io`

Then in repo Settings → Pages → Custom domain: `nospeaky.ai` + Enforce HTTPS.

## Next

- `api.nospeaky.ai` backend: file upload → faster-whisper → VTT/SRT
- Flip `ENGINE_LIVE` and `API_BASE` in `js/app.js`
