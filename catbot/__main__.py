"""Запуск: python -m catbot --config config.toml

Токен бота берётся из переменной окружения CATBOT_TOKEN (выдаёт @BotFather).
Флаг --check проверяет конфиг и показывает план на день без подключения к Telegram.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

from .config import ConfigError, load_config
from .ration import daily_need
from .scheduler import check_shares, day_mode, instances_for_day
from .storage import Store
from . import texts


def _plain(html: str) -> str:
    return re.sub(r"</?(b|i|code)>", "", html).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def check(config_path: str, day: date | None) -> int:
    cfg = load_config(config_path)
    day = day or datetime.now(cfg.timezone).date()
    print(f"Конфиг в порядке: {len(cfg.members)} участника(ов), {len(cfg.cats)} кошки, {len(cfg.tasks)} задач.")
    warnings = check_shares(cfg)
    for w in warnings:
        print(f"⚠️  {w}")
    needs = {c.id: daily_need(cfg, c, day) for c in cfg.cats}
    mode = day_mode(cfg, day)
    print()
    print(_plain(texts.day_summary(cfg, day, mode, [], datetime.combine(day, datetime.min.time(), cfg.timezone))).splitlines()[0])
    for inst in instances_for_day(cfg, day):
        print("\n" + "─" * 40)
        print(_plain(texts.reminder(cfg, inst, cfg.member(inst.task.primary).name, needs, 0)))
    print("\n" + "─" * 40)
    from .bot import plans_by_mode

    print(_plain(texts.ration_text(cfg, needs, {}, plans_by_mode(cfg, day))))
    return 1 if warnings else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="catbot", description="Telegram-бот: напоминания о кормлении и воде для кошек")
    parser.add_argument("--config", default=os.environ.get("CATBOT_CONFIG", "config.toml"))
    parser.add_argument("--check", action="store_true", help="проверить конфиг и показать план без запуска бота")
    parser.add_argument("--date", type=date.fromisoformat, help="дата для --check (ГГГГ-ММ-ДД)")
    args = parser.parse_args(argv)

    try:
        if args.check:
            return check(args.config, args.date)
        cfg = load_config(args.config)
    except (ConfigError, FileNotFoundError) as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return 2

    token = os.environ.get("CATBOT_TOKEN")
    if not token:
        print("Не задан токен: export CATBOT_TOKEN=... (получите у @BotFather)", file=sys.stderr)
        return 2

    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    for w in check_shares(cfg):
        logging.warning(w)

    from .bot import build_application

    plan_text = ""
    plan_path = Path(args.config).resolve().parent / "rules.html"
    if plan_path.exists():
        plan_text = plan_path.read_text(encoding="utf-8")

    store = Store(cfg.db_path)
    app = build_application(token, cfg, store, plan_text)
    try:
        app.run_polling(allowed_updates=["message", "callback_query"])
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
