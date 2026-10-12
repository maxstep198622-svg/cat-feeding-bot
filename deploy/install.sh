#!/usr/bin/env bash
# Установка бота «Стефа и Фая» на сервер Ubuntu 24.04 — одной командой:
#   bash /opt/stefa-i-faya/deploy/install.sh
# Повторный запуск безопасен: токен и семейный код сохраняются, бот перезапускается.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE=catbot
RUN_USER=catbot

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok() { printf '\033[32m✔ %s\033[0m\n' "$*"; }
die() { printf '\n\033[31m✖ %s\033[0m\n\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Нужны права администратора. Запустите так: sudo bash $0"
case "$APP_DIR" in
  /root/* | /home/*) die "Проект лежит в $APP_DIR. Перенесите его в /opt: mv $APP_DIR /opt/stefa-i-faya — и запустите /opt/stefa-i-faya/deploy/install.sh" ;;
esac
cd "$APP_DIR"
[ -f config.toml ] || die "Не найден файл настроек $APP_DIR/config.toml"

say "1/6 Устанавливаю Python…"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv curl >/dev/null
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || die "Нужен Python 3.11 или новее. Переустановите сервер с Ubuntu 24.04."
ok "$(python3 --version)"

say "2/6 Ставлю библиотеки бота…"
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
ok "Библиотеки установлены"

say "3/6 Токен бота…"
TOKEN=""
CODE=""
if [ -f .env ]; then
  TOKEN="$(sed -n 's/^CATBOT_TOKEN=//p' .env | head -n 1)"
  CODE="$(sed -n 's/^CATBOT_JOIN_CODE=//p' .env | head -n 1)"
fi
if [ -n "$TOKEN" ]; then
  ok "Токен уже сохранён. Чтобы заменить его — удалите файл $APP_DIR/.env и запустите установку снова."
else
  echo "Вставьте токен, который прислал @BotFather (вида 123456789:AAH...), и нажмите Enter."
  read -r -p "Токен: " TOKEN
  TOKEN="$(printf '%s' "$TOKEN" | tr -d '[:space:]')"
fi
[[ "$TOKEN" =~ ^[0-9]{5,}:[A-Za-z0-9_-]{30,}$ ]] \
  || die "Это не похоже на токен. Скопируйте его из сообщения @BotFather целиком: цифры, двоеточие и длинная строка."
if [ -z "$CODE" ]; then
  CODE="$(python3 -c 'import secrets, string; print("".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(8)))')"
fi

say "4/6 Проверяю связь с Telegram…"
set +e
RESP="$(curl -sS -m 20 "https://api.telegram.org/bot${TOKEN}/getMe" 2>&1)"
RC=$?
set -e
if [ "$RC" -ne 0 ]; then
  die "Сервер не может связаться с Telegram (${RESP}).
  Скорее всего, сервер находится в России — оттуда доступ к Telegram для ботов заблокирован.
  Нужен сервер за рубежом, например в Нидерландах или Финляндии."
fi
BOT_NAME="$(printf '%s' "$RESP" | python3 -c 'import json, sys
try:
    d = json.load(sys.stdin)
    print(d["result"]["username"] if d.get("ok") else "")
except Exception:
    print("")')"
[ -n "$BOT_NAME" ] || die "Telegram не принял токен. Проверьте, что скопировали его целиком из @BotFather (ответ: ${RESP})"
ok "Бот найден: @${BOT_NAME}"

umask 077
printf 'CATBOT_TOKEN=%s\nCATBOT_JOIN_CODE=%s\n' "$TOKEN" "$CODE" > .env
umask 022

say "5/6 Проверяю настройки…"
CATBOT_JOIN_CODE="$CODE" .venv/bin/python -m catbot --check --config config.toml > /dev/null \
  || die "В файле настроек ошибка — текст выше. Напишите её Claude, он поправит."
ok "Расписание в порядке"

say "6/6 Запускаю бота как службу (он сам поднимется после перезагрузки сервера)…"
id -u "$RUN_USER" > /dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin "$RUN_USER"
mkdir -p data
chown -R "$RUN_USER": data
cat > "/etc/systemd/system/${SERVICE}.service" <<EOF
[Unit]
Description=Telegram-бот «Стефа и Фая»
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
Environment=PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
ExecStart=${APP_DIR}/.venv/bin/python -m catbot --config ${APP_DIR}/config.toml
Restart=always
RestartSec=10
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=${APP_DIR}/data

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable "$SERVICE" > /dev/null 2>&1
systemctl restart "$SERVICE"
sleep 6
if ! systemctl is-active --quiet "$SERVICE"; then
  journalctl -u "$SERVICE" -n 30 --no-pager || true
  die "Бот не запустился — текст ошибки выше. Пришлите его Claude."
fi
ok "Бот работает"

LINK="https://t.me/${BOT_NAME}?start=${CODE}"
cat <<EOF

────────────────────────────────────────────────────────
 🐾 Готово! Бот @${BOT_NAME} работает круглосуточно.

 Ссылка-приглашение для семьи (Папа, Таня, Тася):
   ${LINK}
 Открыть ссылку → «Запустить» → нажать на своё имя.

 Если ссылка не сработает — откройте @${BOT_NAME}, нажмите
 «Запустить» и введите семейный код:  ${CODE}

 Обновить бота после правок:  bash ${APP_DIR}/deploy/update.sh
 Посмотреть, что с ботом:      systemctl status ${SERVICE}
────────────────────────────────────────────────────────
EOF
