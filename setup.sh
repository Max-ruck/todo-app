#!/usr/bin/env bash
# setup.sh  –  Ubuntu 26.04 Deployment für die Todo-App
# Ausführen als root oder mit sudo: bash setup.sh
set -e

APP_DIR="/opt/todoapp"
SERVICE_USER="todoapp"

# ── Konfiguration: vor dem Ausführen anpassen oder als Env-Vars setzen ─────────
SERVER_IP="${SERVER_IP:-DEINE-SERVER-IP}"
DOMAIN="${DOMAIN:-DEINE-DOMAIN.de}"
SERVER_LOCATION="${SERVER_LOCATION:-Dein Hoster, Dein Rechenzentrum}"

echo "=== 1. Pakete aktualisieren ==="
apt-get update -q
apt-get install -y python3 python3-venv python3-pip nginx

echo "=== 2. App-Verzeichnis anlegen ==="
mkdir -p "$APP_DIR"

echo "=== 3. Dateien kopieren (Skript aus gleichem Verzeichnis) ==="
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cp -r "$SCRIPT_DIR/backend"  "$APP_DIR/"
cp -r "$SCRIPT_DIR/frontend" "$APP_DIR/"

echo "=== 4. Python-Venv und Abhängigkeiten ==="
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/backend/requirements.txt"

echo "=== 5. System-User anlegen ==="
id "$SERVICE_USER" &>/dev/null || useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"

echo "=== 6. SECRET_KEY generieren ==="
SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
echo "SECRET_KEY=$SECRET" > "$APP_DIR/backend/.env"
chown "$SERVICE_USER:$SERVICE_USER" "$APP_DIR/backend/.env"
chmod 600 "$APP_DIR/backend/.env"

echo "=== 7. systemd Service ==="
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

echo "=== 8. nginx Konfiguration ==="
cat > /etc/nginx/sites-available/todoapp << EOF
server {
    listen 80;
    server_name $SERVER_IP _;

    client_max_body_size 10M;

    # Redirect / → /todo/
    location = / {
        return 301 /todo/;
    }

    location /todo/ {
        rewrite ^/todo/(.*) /$1 break;
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

echo "=== 9. Datenschutz-Platzhalter ersetzen ==="
INSTALL_DATE="${INSTALL_DATE:-$(date '+%B %Y')}"
DATENSCHUTZ="$APP_DIR/frontend/datenschutz.html"
sed -i "s|{DOMAIN}|$DOMAIN|g"                   "$DATENSCHUTZ"
sed -i "s|{SERVER_LOCATION}|$SERVER_LOCATION|g" "$DATENSCHUTZ"
sed -i "s|{INSTALL_DATE}|$INSTALL_DATE|g"       "$DATENSCHUTZ"

ln -sf /etc/nginx/sites-available/todoapp /etc/nginx/sites-enabled/todoapp
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl restart nginx

echo ""
echo "======================================"
echo " ✅  Todo-App läuft!"
echo "    http://$SERVER_IP/todo/"
echo "======================================"
echo ""
echo "Nützliche Befehle:"
echo "  systemctl status todoapp"
echo "  journalctl -u todoapp -f"
echo "  systemctl restart todoapp"
