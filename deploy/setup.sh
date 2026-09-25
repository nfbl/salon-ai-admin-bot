#!/usr/bin/env bash
# Установка или обновление бота на чистом Ubuntu/Debian (запускать от root).
# Перед первым запуском положите .env в /opt/salon-bot/.env
set -euo pipefail

REPO=https://github.com/nfbl/salon-ai-admin-bot.git
DIR=/opt/salon-bot

# apt на Ubuntu 24.04 (needrestart) читает stdin и съедает остаток скрипта при `bash -s < setup.sh`
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq </dev/null
apt-get install -y -qq git python3 python3-venv >/dev/null </dev/null

id bot >/dev/null 2>&1 || useradd --system --home "$DIR" --shell /usr/sbin/nologin bot

if [ -d "$DIR/.git" ]; then
    # после первой установки папка принадлежит bot, а скрипт идёт от root
    git -c safe.directory="$DIR" -C "$DIR" pull --ff-only
else
    mkdir -p "$DIR"
    # .env мог быть скопирован заранее — клонируем рядом и переносим
    git clone -q "$REPO" /tmp/salon-bot-src
    cp -a /tmp/salon-bot-src/. "$DIR"/
    rm -rf /tmp/salon-bot-src
fi

python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install -q --upgrade pip
"$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"

chown -R bot:bot "$DIR"
chmod 600 "$DIR/.env" 2>/dev/null || echo "ВНИМАНИЕ: нет $DIR/.env — бот не запустится"

SERVICES=()
grep -q '^BOT_TOKEN=.' "$DIR/.env" 2>/dev/null && SERVICES+=(salon-bot)
grep -q '^MAX_BOT_TOKEN=.' "$DIR/.env" 2>/dev/null && SERVICES+=(salon-max-bot)
[ ${#SERVICES[@]} -gt 0 ] || { echo "В $DIR/.env нет ни BOT_TOKEN, ни MAX_BOT_TOKEN"; exit 1; }

for s in "${SERVICES[@]}"; do
    cp "$DIR/deploy/$s.service" "/etc/systemd/system/$s.service"
done
systemctl daemon-reload
for s in "${SERVICES[@]}"; do
    systemctl enable -q "$s"
    systemctl restart "$s"
done
sleep 3
systemctl --no-pager --lines=5 status "${SERVICES[@]}"
