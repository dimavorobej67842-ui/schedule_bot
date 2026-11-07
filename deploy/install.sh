#!/bin/bash
# Установка schedule_bot на чистый Ubuntu/Debian. Запускать от root:
#   bash install.sh
set -e

APP_DIR=/opt/schedule_bot
REPO=https://github.com/dimavorobej67842-ui/schedule_bot.git

echo "== 1/5 Пакеты =="
apt-get update
apt-get install -y python3 python3-venv git

echo "== 2/5 Код =="
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
else
  git clone "$REPO" "$APP_DIR"
fi

echo "== 3/5 Зависимости =="
cd "$APP_DIR"
python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt

echo "== 4/5 Конфигурация =="
if [ ! -f .env ]; then
  cp .env.example .env
  echo ""
  echo "!!! Файл .env создан из шаблона, но пустой."
  echo "!!! Заполни его: nano $APP_DIR/.env  (BOT_TOKEN, CHAT_ID, VK_TOKEN, VK_CHAT_PEER_ID)"
  echo "!!! Затем запусти bash install.sh ещё раз."
  exit 1
fi

echo "== 5/5 Сервис =="
cp deploy/schedule_bot.service /etc/systemd/system/schedule_bot.service
systemctl daemon-reload
systemctl enable --now schedule_bot
sleep 3
systemctl --no-pager status schedule_bot || true
echo ""
echo "Логи: journalctl -u schedule_bot -f"
