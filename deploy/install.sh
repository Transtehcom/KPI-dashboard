#!/usr/bin/env bash
# Установка дашборда KPI на Ubuntu: служба systemd, Caddy с HTTPS, ежедневная копия базы.
# Запуск от root:  sudo bash /opt/kpi-dashboard/deploy/install.sh kpi.transtehcom.ru
# Повторный запуск безопасен: обновляет код из GitHub и перезапускает службу.
set -euo pipefail

DOMAIN="${1:?Укажите домен: sudo bash deploy/install.sh kpi.transtehcom.ru}"
APP_DIR=/opt/kpi-dashboard
APP_USER=kpi
REPO=https://github.com/Transtehcom/KPI-dashboard.git

[ "$(id -u)" -eq 0 ] || { echo "Запустите через sudo"; exit 1; }

echo "== Пакеты"
apt-get update -q
apt-get install -y -q python3 git sqlite3
if ! apt-get install -y -q caddy; then
  # Caddy нет в репозиториях Ubuntu — ставим из официального репозитория Caddy
  apt-get install -y -q debian-keyring debian-archive-keyring apt-transport-https curl gnupg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -q
  apt-get install -y -q caddy
fi

echo "== Пользователь и код"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --home-dir "$APP_DIR" --no-create-home --shell /usr/sbin/nologin "$APP_USER"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
else
  git clone "$REPO" "$APP_DIR"
fi
# код принадлежит root, служба может писать только в data/
mkdir -p "$APP_DIR/data"
chown -R "$APP_USER:$APP_USER" "$APP_DIR/data"
chmod 750 "$APP_DIR/data"

if [ ! -f "$APP_DIR/config.json" ]; then
  cat <<EOF

Нет файла $APP_DIR/config.json (адрес вебхука, сотрудники). Скопируйте его с компьютера, где дашборд работает сейчас:
  scp config.json root@<IP-сервера>:$APP_DIR/config.json
По желанию перенесите и базу (пароли, оценки руководителя, планы):
  scp data/kpi.sqlite root@<IP-сервера>:$APP_DIR/data/kpi.sqlite
Затем запустите этот скрипт ещё раз.
EOF
  exit 1
fi
chown "root:$APP_USER" "$APP_DIR/config.json"
chmod 640 "$APP_DIR/config.json"
[ -f "$APP_DIR/data/kpi.sqlite" ] && chown "$APP_USER:$APP_USER" "$APP_DIR/data/kpi.sqlite"

echo "== Настройки для работы за Caddy"
PORT=$(python3 - "$APP_DIR/config.json" <<'PY'
import json, sys
p = sys.argv[1]
c = json.load(open(p, encoding="utf-8"))
s = c.setdefault("server", {})
s.update(host="127.0.0.1", secure_cookie=True, open_browser=False, trust_proxy=True)
s.setdefault("port", 8081)
json.dump(c, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print(s["port"])
PY
)

echo "== Служба kpi-dashboard"
install -m 644 "$APP_DIR/deploy/kpi-dashboard.service" /etc/systemd/system/kpi-dashboard.service
systemctl daemon-reload
systemctl enable kpi-dashboard >/dev/null
systemctl restart kpi-dashboard

echo "== Caddy"
if grep -q "^# kpi-dashboard" /etc/caddy/Caddyfile 2>/dev/null; then
  echo "/etc/caddy/Caddyfile уже настроен ранее, не перезаписываю"
else
  [ -f /etc/caddy/Caddyfile ] && cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.before-kpi
  sed -e "s/^DOMAIN {/$DOMAIN {/" -e "s/127.0.0.1:PORT/127.0.0.1:$PORT/" "$APP_DIR/deploy/Caddyfile" > /etc/caddy/Caddyfile
fi
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null
systemctl enable caddy >/dev/null
systemctl reload-or-restart caddy

echo "== Ежедневная копия базы (/var/backups/kpi-dashboard, 14 дней)"
cat > /etc/cron.daily/kpi-dashboard-backup <<'EOF'
#!/bin/sh
set -e
dir=/var/backups/kpi-dashboard
mkdir -p "$dir"
chmod 700 "$dir"
sqlite3 /opt/kpi-dashboard/data/kpi.sqlite ".backup '$dir/kpi-$(date +%F).sqlite'"
find "$dir" -name 'kpi-*.sqlite' -mtime +14 -delete
EOF
chmod 755 /etc/cron.daily/kpi-dashboard-backup

if ufw status 2>/dev/null | grep -q "Status: active"; then
  echo "== Брандмауэр: открываю 80 и 443"
  ufw allow 80/tcp >/dev/null
  ufw allow 443/tcp >/dev/null
fi

sleep 3
systemctl --no-pager --lines=5 status kpi-dashboard || true

# пароль demo из демо-режима в интернете недопустим
WEAK=$(cd "$APP_DIR" && sudo -u "$APP_USER" python3 - <<'PY'
from app.common import connect, load_config
from app.server import hash_password
logins = {e["login"] for e in load_config()["employees"]}
con = connect()
print(" ".join(r["login"] for r in con.execute("SELECT login, pwd_hash, salt FROM users")
               if r["login"] in logins and hash_password("demo", r["salt"])[0] == r["pwd_hash"]))
PY
)
if [ -n "$WEAK" ]; then
  echo
  echo "ВНИМАНИЕ: у этих логинов пароль demo, смените его до того, как давать ссылку: $WEAK"
fi
cat <<EOF

Готово. Дашборд: https://$DOMAIN (сертификат появится в течение минуты, если DNS уже указывает на этот сервер).
Задайте пароли (у всех, кому нужен вход):
  cd $APP_DIR && sudo -u $APP_USER python3 manage.py password <логин>
Журнал службы:  journalctl -u kpi-dashboard -f
EOF
