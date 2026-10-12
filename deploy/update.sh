#!/usr/bin/env bash
# Обновление бота «Стефа и Фая»: скачать новую версию с GitHub и перезапустить.
#   bash /opt/stefa-i-faya/deploy/update.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE=catbot

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok() { printf '\033[32m✔ %s\033[0m\n' "$*"; }
die() { printf '\n\033[31m✖ %s\033[0m\n\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Нужны права администратора. Запустите так: sudo bash $0"
cd "$APP_DIR"
[ -f .env ] || die "Бот ещё не установлен. Сначала: bash $APP_DIR/deploy/install.sh"

say "Скачиваю новую версию…"
BEFORE="$(git rev-parse HEAD)"
git pull --ff-only || die "Не удалось скачать обновление. Если просит логин — выполните: gh auth login -h github.com -p https -w && gh auth setup-git"
AFTER="$(git rev-parse HEAD)"
if [ "$BEFORE" = "$AFTER" ]; then
  ok "Обновлений нет — уже последняя версия"
else
  git log --format='  • %s' "${BEFORE}..${AFTER}"
fi

say "Обновляю библиотеки и проверяю настройки…"
.venv/bin/pip install -q -r requirements.txt
CATBOT_JOIN_CODE="$(sed -n 's/^CATBOT_JOIN_CODE=//p' .env | head -n 1)" \
  .venv/bin/python -m catbot --check --config config.toml > /dev/null \
  || die "В новых настройках ошибка — бот НЕ перезапущен и работает по-старому. Пришлите текст ошибки Claude."
ok "Настройки в порядке"

say "Перезапускаю бота…"
systemctl restart "$SERVICE"
sleep 5
if ! systemctl is-active --quiet "$SERVICE"; then
  journalctl -u "$SERVICE" -n 30 --no-pager || true
  die "Бот не запустился — текст ошибки выше. Пришлите его Claude."
fi
ok "Готово, бот работает на новой версии"
