#!/usr/bin/env bash
# install.sh – Vollständiges Setup der Todo-App auf einem frischen Ubuntu-Server
# Aufruf: bash install.sh [domain] [server-standort] [admin-email]
#         Fehlende Werte werden interaktiv abgefragt.
#
# Voraussetzungen:
#   - Ubuntu 24.04 LTS
#   - Root-Zugriff oder sudo
#   - Domain zeigt bereits per A-Record auf diesen Server
set -euo pipefail

APP_DIR="/opt/todoapp"
SERVICE_USER="todoapp"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── Argumente oder interaktive Eingabe ────────────────────────────────────────
DOMAIN="${1:-}"
SERVER_LOCATION="${2:-}"
ADMIN_EMAIL="${3:-}"

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║   Todo-App Installer                         ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

# Domain (Pflicht)
if [[ -z "$DOMAIN" ]]; then
  read -rp "Domain (z.B. meinedomain.de): " DOMAIN
fi
if [[ -z "$DOMAIN" ]]; then
  echo "Fehler: Domain ist erforderlich. Abbruch."
  exit 1
fi

# Serverstandort (Default: Hetzner, Helsinki FI)
if [[ -z "$SERVER_LOCATION" ]]; then
  read -rp "Serverstandort für Datenschutzerklärung (z.B. Hetzner, Frankfurt DE): " SERVER_LOCATION
fi
SERVER_LOCATION="${SERVER_LOCATION:-Unbekannter Standort}"

# Admin-Email für Certbot (Default: admin@domain)
if [[ -z "$ADMIN_EMAIL" ]]; then
  read -rp "Admin-E-Mail für SSL-Zertifikat [admin@${DOMAIN}]: " ADMIN_EMAIL
fi
ADMIN_EMAIL="${ADMIN_EMAIL:-admin@${DOMAIN}}"

echo ""
echo "  Domain:      $DOMAIN"
echo "  Standort:    $SERVER_LOCATION"
echo "  Admin-Email: $ADMIN_EMAIL"
echo ""
read -rp "Weiter mit Installation? [j/N] " CONFIRM
if [[ ! "$CONFIRM" =~ ^[jJyY]$ ]]; then
  echo "Abgebrochen."
  exit 0
fi
echo ""

# ── 1. Pakete ──────────────────────────────────────────────────────────────────
echo "=== 1. Pakete installieren ==="
apt-get update -q
apt-get install -y python3 python3-venv python3-pip nginx certbot python3-certbot-nginx

# ── 2. App-Verzeichnis anlegen ─────────────────────────────────────────────────
echo "=== 2. Verzeichnis anlegen ==="
mkdir -p "$APP_DIR"

# ── 3. Dateien kopieren ────────────────────────────────────────────────────────
echo "=== 3. Dateien kopieren ==="
cp -r "$SCRIPT_DIR/backend"  "$APP_DIR/"
cp -r "$SCRIPT_DIR/frontend" "$APP_DIR/"

# ── 4. Python-venv ────────────────────────────────────────────────────────────
echo "=== 4. Python-Umgebung einrichten ==="
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/backend/requirements.txt"

# ── 5. System-User anlegen ────────────────────────────────────────────────────
echo "=== 5. System-User anlegen ==="
id "$SERVICE_USER" &>/dev/null || \
  useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"

# ── 6. .env generieren ────────────────────────────────────────────────────────
echo "=== 6. .env generieren ==="
SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
INSTALL_DATE="$(date '+%B %Y')"

cat > "$APP_DIR/backend/.env" << EOF
SECRET_KEY=$SECRET
DATABASE_URL=todo.db
BASE_URL=https://$DOMAIN/todo
EOF

chown "$SERVICE_USER:$SERVICE_USER" "$APP_DIR/backend/.env"
chmod 600 "$APP_DIR/backend/.env"

# ── 7. Datenschutz-Platzhalter ersetzen ───────────────────────────────────────
echo "=== 7. Datenschutz-Platzhalter ersetzen ==="
DATENSCHUTZ="$APP_DIR/frontend/datenschutz.html"
sed -i "s|{DOMAIN}|$DOMAIN|g"                   "$DATENSCHUTZ"
sed -i "s|{SERVER_LOCATION}|$SERVER_LOCATION|g" "$DATENSCHUTZ"
sed -i "s|{INSTALL_DATE}|$INSTALL_DATE|g"       "$DATENSCHUTZ"

# ── 8. Rechte setzen ──────────────────────────────────────────────────────────
echo "=== 8. Dateiberechtigungen ==="
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"

# ── 9. systemd Service ────────────────────────────────────────────────────────
echo "=== 9. systemd Service einrichten ==="
cat > /etc/systemd/system/todoapp.service << EOF
[Unit]
Description=Todo-App FastAPI Backend
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$APP_DIR/backend
EnvironmentFile=$APP_DIR/backend/.env
ExecStart=$APP_DIR/venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000 --workers 2
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable todoapp
systemctl start todoapp

# ── 10. nginx Konfiguration ───────────────────────────────────────────────────
echo "=== 10. nginx konfigurieren ==="
cat > /etc/nginx/sites-available/todoapp << EOF
server {
    listen 80;
    server_name $DOMAIN;

    client_max_body_size 10M;

    location = / {
        return 301 /todo/;
    }

    location /todo/ {
        rewrite ^/todo/(.*) /\$1 break;
        proxy_pass         http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header   Host              \$host;
        proxy_set_header   X-Real-IP         \$remote_addr;
        proxy_set_header   X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto \$scheme;
        proxy_read_timeout 60s;
    }
}
EOF

ln -sf /etc/nginx/sites-available/todoapp /etc/nginx/sites-enabled/todoapp
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl restart nginx

# ── 11. SSL-Zertifikat via Certbot ────────────────────────────────────────────
echo "=== 11. SSL-Zertifikat einrichten ==="
certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos \
  --email "$ADMIN_EMAIL" --redirect

# ── 12. Cronjob: Certbot Auto-Renewal ─────────────────────────────────────────
echo "=== 12. Certbot-Cronjob einrichten ==="
CRON_CMD="0 3 * * * root certbot renew --quiet --post-hook 'systemctl reload nginx'"
if ! grep -q "certbot renew" /etc/crontab 2>/dev/null; then
  echo "$CRON_CMD" >> /etc/crontab
fi

# ── 13. Abschluss ─────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║   ✅  Installation abgeschlossen!            ║"
echo "╚══════════════════════════════════════════════╝"
echo ""
echo "  App erreichbar unter: https://$DOMAIN/todo/"
echo ""
echo "Nützliche Befehle:"
echo "  systemctl status todoapp        – Service-Status"
echo "  journalctl -u todoapp -f        – Live-Logs"
echo "  systemctl restart todoapp       – Neu starten"
echo "  nginx -t && systemctl reload nginx  – nginx neu laden"
echo ""
echo "Erster Schritt: Öffne https://$DOMAIN/todo/ und erstelle"
echo "deinen Admin-Account (erster Nutzer erhält automatisch Admin-Rechte)."
echo ""
