#!/usr/bin/env bash
# Bootstrap NoSpeaky engine on a fresh Ubuntu/Debian cloud VM.
# Run AS ROOT on the server after SSH in:
#   curl -sL ... | bash
# or copy this file up and: bash bootstrap-vps.sh
set -euo pipefail

APP_USER="${APP_USER:-nospeaky}"
APP_DIR="${APP_DIR:-/opt/nospeaky}"
DOMAIN="${NOSPEAKY_API_DOMAIN:-api.nospeaky.ai}"
API_KEY="${NOSPEAKY_API_KEY:-}"
GIT_URL="${NOSPEAKY_GIT_URL:-https://github.com/MattCrossingham/nospeaky.git}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root"
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl git ufw fail2ban docker.io docker-compose-v2 nginx certbot python3-certbot-nginx
systemctl enable --now docker
systemctl enable --now fail2ban

# firewall: ssh + http/https only
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

id -u "$APP_USER" >/dev/null 2>&1 || useradd -m -s /bin/bash "$APP_USER"
usermod -aG docker "$APP_USER"

mkdir -p "$APP_DIR"
if [[ ! -d "$APP_DIR/repo/.git" ]]; then
  git clone "$GIT_URL" "$APP_DIR/repo"
else
  git -C "$APP_DIR/repo" pull --ff-only || true
fi

if [[ -z "$API_KEY" ]]; then
  API_KEY="$(openssl rand -hex 24)"
fi
umask 077
cat > "$APP_DIR/.env" <<EOF
NOSPEAKY_API_KEY=$API_KEY
NOSPEAKY_BACKEND=faster
NOSPEAKY_MODEL=Systran/faster-whisper-small
NOSPEAKY_DEVICE=cpu
NOSPEAKY_COMPUTE_TYPE=int8
NOSPEAKY_MAX_UPLOAD_MB=200
NOSPEAKY_MAX_DURATION_SEC=600
NOSPEAKY_JOB_LIMIT_PER_HOUR=8
NOSPEAKY_ALLOW_URL_FETCH=0
EOF
chmod 600 "$APP_DIR/.env"

cat > "$APP_DIR/docker-compose.yml" <<EOF
services:
  engine:
    build:
      context: $APP_DIR/repo
      dockerfile: Dockerfile
    restart: unless-stopped
    env_file:
      - $APP_DIR/.env
    ports:
      - "127.0.0.1:8788:8788"
    volumes:
      - $APP_DIR/data:/app/engine/data
    security_opt:
      - no-new-privileges:true
    read_only: false
    tmpfs:
      - /tmp
EOF

mkdir -p "$APP_DIR/data"
chown -R "$APP_USER":"$APP_USER" "$APP_DIR"

cd "$APP_DIR"
docker compose build
docker compose up -d

# nginx reverse proxy
cat > /etc/nginx/sites-available/nospeaky-api <<EOF
server {
    listen 80;
    server_name $DOMAIN;

    client_max_body_size 220m;

    location / {
        proxy_pass http://127.0.0.1:8788;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
EOF
ln -sfn /etc/nginx/sites-available/nospeaky-api /etc/nginx/sites-enabled/nospeaky-api
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

echo "===================================================="
echo "Engine up on 127.0.0.1:8788 behind nginx :80"
echo "Domain: $DOMAIN"
echo "API key: $API_KEY"
echo
echo "DNS: create A record $DOMAIN -> this server public IP"
echo "Then run:"
echo "  certbot --nginx -d $DOMAIN --non-interactive --agree-tos -m YOU@EMAIL --redirect"
echo "===================================================="
