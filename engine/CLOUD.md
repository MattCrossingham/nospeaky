# Cloud deploy (home network stays closed)

## Rule
NoSpeaky engine runs **only in cloud**. Never tunnel from Mac. Never use pi-node.

## Shape
- `nospeaky.ai` = GitHub Pages (static)
- `api.nospeaky.ai` = cloud VM engine (Docker + nginx + HTTPS)
- Home LAN = zero inbound ports for this product

## One-time setup
1. Create a small Ubuntu 24.04 VPS (Hetzner CX22 / DO 2GB+ recommended)
2. DNS: `api.nospeaky.ai` A record → VPS public IP
3. SSH in as root and run `engine/bootstrap-vps.sh`
4. `certbot --nginx -d api.nospeaky.ai ...`
5. Put API URL + key into `js/config.js` and ship site

## Security defaults in bootstrap
- UFW: 22/80/443 only
- fail2ban on
- Docker engine bound to localhost; nginx public
- API key required
- URL fetch OFF by default on cloud (file upload first)
- Rate limit + duration cap

## Matt action needed
Cloud API token **or** SSH access to a fresh VPS. No credentials are stored in this repo yet.
