#!/usr/bin/env bash
# Deploy the Withaq CRM backend and database on this Lightsail server.
#
# Run from the project directory on the server:
#   sudo bash scripts/deploy_lightsail.sh
#
# What it does:
#   1. Installs Python, PostgreSQL, and Nginx packages if they are missing.
#   2. Creates a local PostgreSQL database and user, unless .env already points at a local one.
#   3. Copies the existing data into the local database when .env points at a remote
#      database (such as Render), or when SOURCE_DATABASE_URL is set.
#   4. Runs FastAPI with uvicorn as the systemd service "withaq-api" on 127.0.0.1:8001.
#   5. Routes https://withaq.me/api/ to that service and serves frontend/ as the site root.
#   6. Disables any older uvicorn service running the same app.
#
# Optional environment variables:
#   SOURCE_DATABASE_URL  postgres://user:pass@host:port/db to copy data from
#   DOMAIN               defaults to withaq.me
#   API_PORT             defaults to 8001
set -euo pipefail

DOMAIN="${DOMAIN:-withaq.me}"
API_PORT="${API_PORT:-8001}"
SERVICE_NAME="withaq-api"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$PROJECT_DIR/.env"
RUN_USER="${SUDO_USER:-$(stat -c %U "$PROJECT_DIR")}"
STAMP="$(date +%Y%m%d-%H%M%S)"

log() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!! %s\033[0m\n' "$*"; }
die() { printf '\033[1;31mXX %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run with sudo: sudo bash scripts/deploy_lightsail.sh"
[[ -f "$PROJECT_DIR/backend/main.py" ]] || die "backend/main.py not found in $PROJECT_DIR"
grep -q 'onrender' "$PROJECT_DIR/frontend/app.js" \
  && die "frontend/app.js still points at Render. Pull the latest code first: git pull"

# Read one KEY=value from .env without sourcing the file.
env_get() { [[ -f "$ENV_FILE" ]] && grep -E "^$1=" "$ENV_FILE" | tail -n1 | cut -d= -f2- || true; }

# Set or add one KEY=value in .env.
env_set() {
  if grep -qE "^$1=" "$ENV_FILE"; then
    python3 - "$ENV_FILE" "$1" "$2" <<'PY'
import sys
path, key, value = sys.argv[1:]
lines = open(path, encoding="utf-8").read().splitlines()
lines = [f"{key}={value}" if line.startswith(f"{key}=") else line for line in lines]
open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
PY
  else
    printf '%s=%s\n' "$1" "$2" >> "$ENV_FILE"
  fi
}

# ---------------------------------------------------------------- packages
log "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip postgresql postgresql-client nginx >/dev/null
systemctl enable --now postgresql >/dev/null

# ---------------------------------------------------------------- database
OLD_HOST="$(env_get PGHOST)"
SOURCE_URL="${SOURCE_DATABASE_URL:-}"
if [[ -z "$SOURCE_URL" && -n "$OLD_HOST" && "$OLD_HOST" != "localhost" && "$OLD_HOST" != "127.0.0.1" ]]; then
  # .env currently points at a remote database: copy it before switching to local.
  SOURCE_URL="host=$OLD_HOST port=$(env_get PGPORT) dbname=$(env_get PGDATABASE) user=$(env_get PGUSER) password=$(env_get PGPASSWORD) sslmode=require"
fi

if [[ -n "$OLD_HOST" && ( "$OLD_HOST" == "localhost" || "$OLD_HOST" == "127.0.0.1" ) && -z "${SOURCE_DATABASE_URL:-}" ]]; then
  log "Keeping the existing local database ($(env_get PGDATABASE) on $OLD_HOST)"
else
  DB_NAME="withaq"
  DB_USER="withaq"
  DB_PASS="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"

  log "Creating local PostgreSQL database '$DB_NAME'"
  sudo -u postgres psql -v ON_ERROR_STOP=1 -q <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '$DB_USER') THEN
    CREATE ROLE $DB_USER LOGIN PASSWORD '$DB_PASS';
  ELSE
    ALTER ROLE $DB_USER WITH LOGIN PASSWORD '$DB_PASS';
  END IF;
END \$\$;
SQL
  if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" | grep -q 1; then
    sudo -u postgres createdb -O "$DB_USER" "$DB_NAME"
  fi

  if [[ -n "$SOURCE_URL" ]]; then
    log "Copying data from the remote database into '$DB_NAME'"
    DUMP="/root/withaq-source-$STAMP.sql"
    pg_dump --no-owner --no-acl --clean --if-exists -d "$SOURCE_URL" > "$DUMP" \
      || die "pg_dump failed. Check the source database URL and that its PostgreSQL version is not newer than $(pg_dump --version)."
    PGPASSWORD="$DB_PASS" psql -q -v ON_ERROR_STOP=1 -h 127.0.0.1 -U "$DB_USER" -d "$DB_NAME" -f "$DUMP" >/dev/null
    echo "   Backup of the source data kept at $DUMP"
  fi

  [[ -f "$ENV_FILE" ]] && cp "$ENV_FILE" "$ENV_FILE.bak-$STAMP"
  touch "$ENV_FILE"
  env_set PGHOST 127.0.0.1
  env_set PGPORT 5432
  env_set PGDATABASE "$DB_NAME"
  env_set PGUSER "$DB_USER"
  env_set PGPASSWORD "$DB_PASS"
fi
env_set FRONTEND_ORIGINS "https://$DOMAIN,https://www.$DOMAIN"
grep -q '^ROOT_PATH=' "$ENV_FILE" && sed -i '/^ROOT_PATH=/d' "$ENV_FILE"
chown "$RUN_USER": "$ENV_FILE"
chmod 600 "$ENV_FILE"

# ---------------------------------------------------------------- python app
log "Installing Python requirements"
sudo -u "$RUN_USER" python3 -m venv "$PROJECT_DIR/.venv"
sudo -u "$RUN_USER" "$PROJECT_DIR/.venv/bin/pip" install -q --upgrade pip
sudo -u "$RUN_USER" "$PROJECT_DIR/.venv/bin/pip" install -q -r "$PROJECT_DIR/requirements.txt"

log "Creating systemd service '$SERVICE_NAME' on 127.0.0.1:$API_PORT"
cat > "/etc/systemd/system/$SERVICE_NAME.service" <<UNIT
[Unit]
Description=Withaq CRM API (FastAPI)
After=network.target postgresql.service
Requires=postgresql.service

[Service]
User=$RUN_USER
WorkingDirectory=$PROJECT_DIR
ExecStart=$PROJECT_DIR/.venv/bin/uvicorn backend.main:app --host 127.0.0.1 --port $API_PORT --proxy-headers --forwarded-allow-ips 127.0.0.1
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null
systemctl restart "$SERVICE_NAME"

for _ in $(seq 1 20); do
  curl -fsS "http://127.0.0.1:$API_PORT/api/health" >/dev/null 2>&1 && break
  sleep 1
done
curl -fsS "http://127.0.0.1:$API_PORT/api/health" >/dev/null \
  || { journalctl -u "$SERVICE_NAME" -n 40 --no-pager; die "API did not start. See the log above."; }
echo "   API healthy on port $API_PORT"

# ---------------------------------------------------------------- old services
log "Disabling older uvicorn services for this app"
# Only services that run this project directory: staging runs the same app from another
# directory (withaq-staging-api) and must keep running.
for unit in $(systemctl list-units --type=service --all --no-legend --plain | awk '{print $1}'); do
  [[ "$unit" == "$SERVICE_NAME.service" ]] && continue
  unit_text="$(systemctl cat "$unit" 2>/dev/null || true)"
  if grep -q 'backend.main:app' <<<"$unit_text" \
    && grep -qE "^WorkingDirectory=$PROJECT_DIR/?(backend/?)?\$" <<<"$unit_text"; then
    systemctl disable --now "$unit" >/dev/null 2>&1 && echo "   Disabled $unit"
  fi
done
stray=""
for pid in $(pgrep -f 'uvicorn .*backend.main:app' || true); do
  cwd="$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)"
  [[ "$cwd" == "$PROJECT_DIR" || "$cwd" == "$PROJECT_DIR/backend" ]] || continue
  tr '\0' ' ' < "/proc/$pid/cmdline" | grep -q -- "--port $API_PORT" && continue
  stray+="   $pid $(tr '\0' ' ' < "/proc/$pid/cmdline")"$'\n'
done
if [[ -n "$stray" ]]; then
  warn "A manually started uvicorn is still running (not managed by systemd):"
  printf '%s' "$stray"
  warn "Stop it with: sudo kill <PID>   (Nginx no longer sends traffic to it)"
fi

# ---------------------------------------------------------------- nginx
log "Configuring Nginx for $DOMAIN"
# -R follows the symlinks in sites-enabled; fall back to the file that already proxies /api/.
NGINX_PATHS=(/etc/nginx/sites-enabled/ /etc/nginx/conf.d/ /etc/nginx/nginx.conf)
SITE_FILE="$(grep -RlE "server_name[^;]*\b${DOMAIN//./\\.}\b" "${NGINX_PATHS[@]}" 2>/dev/null | head -n1 || true)"
[[ -n "$SITE_FILE" ]] || SITE_FILE="$(grep -RlE "location\s+(\^~\s+)?/api/?\s*\{" "${NGINX_PATHS[@]}" 2>/dev/null | head -n1 || true)"
[[ -n "$SITE_FILE" ]] || die "No Nginx config for $DOMAIN found. Send the output of: sudo nginx -T | grep -n -E 'server_name|listen|# configuration file'"
SITE_FILE="$(readlink -f "$SITE_FILE")"
cp "$SITE_FILE" "/root/nginx-$(basename "$SITE_FILE")-$STAMP.bak"
echo "   Site file: $SITE_FILE (backup in /root/)"

mkdir -p /etc/nginx/snippets
cat > /etc/nginx/snippets/withaq-api.conf <<NGINX
location /api/ {
    proxy_pass http://127.0.0.1:$API_PORT;
    proxy_http_version 1.1;
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
    proxy_read_timeout 60s;
}
NGINX

python3 - "$SITE_FILE" "$PROJECT_DIR/frontend" <<'PY'
import re, sys
path, frontend = sys.argv[1:]
text = open(path, encoding="utf-8").read()

# Remove every existing /api location; the snippet replaces them.
text = re.sub(r"\n?[ \t]*location\s+(?:\^~\s+)?/api/?\s*\{[^{}]*\}", "", text)
text = re.sub(r"\n?[ \t]*include\s+snippets/withaq-api\.conf;", "", text)

def patch_server(block: str) -> str:
    if not re.search(r"listen\s+[^;]*443", block):
        return block
    # Serve the project's frontend directory as the site root.
    if re.search(r"^\s*root\s+[^;]+;", block, flags=re.M):
        block = re.sub(r"^(\s*)root\s+[^;]+;", rf"\1root {frontend};", block, count=1, flags=re.M)
    else:
        block = re.sub(r"(server_name[^;]*;)", rf"\1\n    root {frontend};", block, count=1)
    if not re.search(r"^\s*index\s+", block, flags=re.M):
        block = re.sub(r"(root [^;]+;)", r"\1\n    index index.html;", block, count=1)
    return re.sub(r"(server_name[^;]*;)", r"\1\n    include snippets/withaq-api.conf;", block, count=1)

# Patch each top-level server { ... } block.
out, i = [], 0
for match in re.finditer(r"server\s*\{", text):
    if match.start() < i:
        continue
    depth, j = 0, match.end() - 1
    while j < len(text):
        depth += {"{": 1, "}": -1}.get(text[j], 0)
        if depth == 0:
            break
        j += 1
    out.append(text[i:match.start()])
    out.append(patch_server(text[match.start():j + 1]))
    i = j + 1
out.append(text[i:])
open(path, "w", encoding="utf-8").write("".join(out))
PY

grep -q 'include snippets/withaq-api.conf' "$SITE_FILE" \
  || { cp "/root/nginx-$(basename "$SITE_FILE")-$STAMP.bak" "$SITE_FILE"; die "No HTTPS (listen 443) server block found for $DOMAIN. Nginx left unchanged."; }

# Nginx must be able to read the frontend files.
dir="$PROJECT_DIR/frontend"
while [[ "$dir" != "/" ]]; do chmod o+x "$dir"; dir="$(dirname "$dir")"; done
chmod -R o+r "$PROJECT_DIR/frontend"

if nginx -t 2>/dev/null; then
  systemctl reload nginx
else
  nginx -t || true
  cp "/root/nginx-$(basename "$SITE_FILE")-$STAMP.bak" "$SITE_FILE"
  die "Nginx config test failed; the previous config was restored."
fi

# ---------------------------------------------------------------- verify
log "Verifying https://$DOMAIN"
sleep 1
health="$(curl -fsS "https://$DOMAIN/api/health" || true)"
setup="$(curl -fsS "https://$DOMAIN/api/auth/setup-status" || true)"
users="$(sudo -u postgres psql -tA -d "$(env_get PGDATABASE)" -c 'SELECT count(*) FROM users' 2>/dev/null || echo '?')"
echo "   /api/health            -> ${health:-FAILED}"
echo "   /api/auth/setup-status -> ${setup:-FAILED}"
echo "   accounts in local DB   -> $users"
[[ "$health" == *'"ok"'* ]] || die "HTTPS API check failed. Run: sudo journalctl -u $SERVICE_NAME -n 50"

log "Done. Backend and database now run on this server."
echo "   Service logs:  sudo journalctl -u $SERVICE_NAME -f"
echo "   Restart API:   sudo systemctl restart $SERVICE_NAME"
echo "   Database:      $(env_get PGDATABASE) on $(env_get PGHOST) (credentials in $ENV_FILE)"
