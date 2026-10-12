"""Регрессии по находкам код-ревью."""

import asyncio
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

import pytest
from telegram.error import TimedOut

from catbot.config import ConfigError, parse_config
from catbot.scheduler import State, decide, instances_for_day
from test_bot_flow import CHATS, MON, Clock, FakeBot, Message, lunch_id, press, say, tick, to

from catbot.bot import CatBot
from catbot.storage import Store


@pytest.fixture
def env(cfg):
    store = Store(":memory:")
    bot = CatBot(cfg, store)
    clock = Clock(cfg.timezone)
    bot.now = lambda: clock.value
    app = SimpleNamespace(bot=FakeBot())
    for member, chat in CHATS.items():
        store.bind_member(member, chat, None, clock.set(7, 0))
    return SimpleNamespace(bot=bot, store=store, clock=clock, app=app, fake=app.bot)


def run_cmd(env, member, handler, args=None):
    msg = Message("", CHATS[member])
    update = SimpleNamespace(
        effective_message=msg,
        effective_chat=SimpleNamespace(id=CHATS[member], type="private"),
        effective_user=SimpleNamespace(id=CHATS[member] * 1000, username=None),
    )
    ctx = SimpleNamespace(application=env.app, user_data={}, args=args or [])
    asyncio.run(handler(update, ctx))
    return msg


# --- смена режима посреди дня ---


def test_mode_switch_midday_does_not_revive_past_slots(env):
    tick(env, 7, 45)
    env.clock.set(7, 50)
    press(env, "papa", f"done|{MON}|breakfast")
    env.clock.set(9, 0)
    press(env, "tanya", f"mode|{MON}:weekend")
    recs = {r.task_id: r.status for r in env.store.for_day(MON)}
    assert recs["breakfast"] == "done"
    assert recs["wk_breakfast"] == "skipped"  # 08:30 уже прошло — не предлагаем кормить второй раз
    assert recs["wk_lunch"] == "pending"
    before = len(env.fake.sent)
    tick(env, 9, 1)
    tick(env, 10, 0)
    new = [m.text for m in env.fake.sent[before:]]
    assert not any("пора покормить" in t or "ещё не отмечено" in t or "так и не отмечено" in t for t in new)


def test_mode_switch_cancels_started_task_and_closes_messages(env):
    tick(env, 14, 30)  # обед отправлен Тасе
    env.clock.set(14, 35)
    press(env, "tanya", f"mode|{MON}:weekend")
    assert env.store.get(lunch_id()).status == "skipped"
    assert any("сменили режим" in e[2] for e in env.fake.edited)
    before = len(env.fake.sent)
    tick(env, 15, 10)  # ни повторов, ни эскалации по отменённой задаче
    assert not any("Обед" in m.text and "14:30" in m.text for m in env.fake.sent[before:])


def test_done_task_from_old_mode_still_gets_followup(raw):
    raw["tasks"][1]["days"] = ["weekday"]
    cfg = parse_config(raw)
    store = Store(":memory:")
    bot = CatBot(cfg, store)
    clock = Clock(cfg.timezone)
    bot.now = lambda: clock.value
    e = SimpleNamespace(bot=bot, store=store, clock=clock, app=SimpleNamespace(bot=FakeBot()))
    e.fake = e.app.bot
    for m, c in CHATS.items():
        store.bind_member(m, c, None, clock.set(7, 0))
    tick(e, 14, 30)
    clock.set(14, 32)
    press(e, "tasya", f"done|{lunch_id()}")
    clock.set(14, 40)
    press(e, "tanya", f"mode|{MON}:weekend")
    tick(e, 15, 2)
    assert any("прошло полчаса" in m.text for m in to(e, "tasya"))


# --- кнопки на эскалации ---


def test_escalation_has_only_done_button_and_snooze_is_owner_only(env):
    for hh, mm in [(14, 30), (14, 40), (14, 50), (15, 0)]:
        tick(env, hh, mm)
    esc = [m for m in to(env, "tanya") if "ещё не отмечено" in m.text][0]
    buttons = [b.callback_data for row in esc.markup.inline_keyboard for b in row]
    assert buttons == [f"done|{lunch_id()}"]
    env.clock.set(15, 1)
    q = press(env, "tanya", f"snooze|{lunch_id()}")
    assert "для: Тася" in q.answers[0]
    q = press(env, "tanya", f"pass|{lunch_id()}")
    assert "для: Тася" in q.answers[0]
    assert env.store.get(lunch_id()).assignee == "tasya"


def test_snooze_after_home_pull_fires_before_due(env):
    env.clock.set(12, 0)
    say(env, "tasya", "🏠 Я дома")
    env.clock.set(12, 1)
    press(env, "tasya", f"snooze|{lunch_id()}")
    tick(env, 12, 5)
    assert len([m for m in to(env, "tasya") if "Обед" in m.text]) == 1
    tick(env, 12, 11)
    assert len([m for m in to(env, "tasya") if "Обед" in m.text]) == 2


# --- сбои сети ---


def test_transient_failure_is_retried(env):
    for hh, mm in [(14, 30), (14, 40), (14, 50)]:
        tick(env, hh, mm)
    env.fake.fail_with = TimedOut()
    tick(env, 15, 0)
    assert env.store.get(lunch_id()).escalated_at is None
    env.fake.fail_with = None
    tick(env, 15, 1)
    assert env.store.get(lunch_id()).escalated_at is not None
    assert any("ещё не отмечено" in m.text for m in to(env, "tanya"))


def test_transient_failure_on_first_reminder_is_not_counted(env):
    env.fake.fail_with = TimedOut()
    tick(env, 14, 30)
    assert env.store.get(lunch_id()).sent_count == 0
    assert not to(env, "tanya")  # не поднимаем ложную тревогу родителям
    env.fake.fail_with = None
    tick(env, 14, 31)
    assert env.store.get(lunch_id()).sent_count == 1 and len(to(env, "tasya")) == 1


def test_unregistered_assignee_fallback_goes_to_backup_once(env):
    env.store.unbind_chat(CHATS["tasya"])
    for hh, mm in [(14, 30), (14, 40), (15, 0), (15, 30)]:
        tick(env, hh, mm)
    tanya = [m for m in to(env, "tanya") if "ещё не отмечено" in m.text]
    assert len(tanya) == 1 and "не подключён" in tanya[0].text
    assert len([m for m in to(env, "papa") if "ещё не отмечено" in m.text]) == 1


# --- полночь ---


def test_followup_after_midnight_for_late_task(raw):
    raw["tasks"][2]["time"] = "23:40"
    raw["tasks"][2]["followup"] = True
    cfg = parse_config(raw)
    store = Store(":memory:")
    bot = CatBot(cfg, store)
    clock = Clock(cfg.timezone)
    bot.now = lambda: clock.value
    e = SimpleNamespace(bot=bot, store=store, clock=clock, app=SimpleNamespace(bot=FakeBot()))
    e.fake = e.app.bot
    for m, c in CHATS.items():
        store.bind_member(m, c, None, clock.set(7, 0))
    tick(e, 23, 40)
    clock.set(23, 50)
    press(e, "tasya", f"done|{MON}|dinner")
    tick(e, 0, 21, day=MON + timedelta(days=1))
    assert any("прошло полчаса" in m.text for m in to(e, "tasya"))


def test_escalation_after_midnight(raw):
    raw["tasks"].append({"id": "late", "name": "Поздняя проверка", "time": "23:50", "days": ["all"],
                         "primary": "tasya", "backup": "tanya", "escalate_after_min": 30, "give_up_after_min": 120})
    cfg = parse_config(raw)
    store = Store(":memory:")
    bot = CatBot(cfg, store)
    clock = Clock(cfg.timezone)
    bot.now = lambda: clock.value
    e = SimpleNamespace(bot=bot, store=store, clock=clock, app=SimpleNamespace(bot=FakeBot()))
    e.fake = e.app.bot
    for m, c in CHATS.items():
        store.bind_member(m, c, None, clock.set(7, 0))
    tick(e, 23, 50)
    tick(e, 0, 20, day=MON + timedelta(days=1))
    assert any("Поздняя проверка" in m.text and "ещё не отмечено" in m.text for m in to(e, "tanya"))


# --- отметки ---


def test_retroactive_done_on_missed_task_skips_followup(env):
    for hh, mm in [(14, 30), (14, 40), (14, 50), (15, 0), (17, 0)]:
        tick(env, hh, mm)
    assert env.store.get(lunch_id()).status == "missed"
    env.clock.set(18, 0)
    press(env, "tanya", f"done|{lunch_id()}")
    tick(env, 18, 31)
    assert not any("прошло полчаса" in m.text for m in env.fake.sent)


def test_done_rejected_for_other_day_and_far_future(env):
    tick(env, 6, 0)
    env.clock.set(6, 0)
    q = press(env, "tasya", f"done|{lunch_id()}")  # обед в 14:30 — до него больше 3 часов
    assert "рано" in q.answers[0]
    tick(env, 14, 30)
    env.clock.set(9, 0, day=MON + timedelta(days=1))
    q = press(env, "tasya", f"done|{lunch_id()}")
    assert "12.10" in q.answers[0]
    assert env.store.get(lunch_id()).status != "done"


def test_today_buttons_child_sees_only_own_due_tasks(env):
    tick(env, 14, 0)
    env.clock.set(14, 0)
    msg = run_cmd(env, "tasya", env.bot.cmd_today)
    assert msg.replies  # без исключений
    env.clock.set(14, 0)
    captured = {}

    async def reply_text(text, parse_mode=None, reply_markup=None):
        captured["markup"] = reply_markup

    m = Message("", CHATS["tasya"])
    m.reply_text = reply_text
    update = SimpleNamespace(effective_message=m, effective_chat=SimpleNamespace(id=CHATS["tasya"], type="private"),
                             effective_user=SimpleNamespace(id=1, username=None))
    asyncio.run(env.bot.cmd_today(update, SimpleNamespace(application=env.app, user_data={}, args=[])))
    data = [b.callback_data for row in captured["markup"].inline_keyboard for b in row]
    assert data == [f"done|{lunch_id()}"]  # завтрак — папин, ужин в 19:00 тоже не её


def test_today_survives_removed_task(env, raw):
    tick(env, 14, 30)
    raw["tasks"] = [t for t in raw["tasks"] if t["id"] != "lunch"]
    env.bot.cfg = parse_config(raw)
    env.clock.set(14, 40)
    msg = run_cmd(env, "tasya", env.bot.cmd_today)
    assert msg.replies
    msg = run_cmd(env, "tasya", env.bot.cmd_home)
    assert msg.replies
    assert env.store.get(lunch_id()).status == "skipped"


# --- возраст и вес ---


def test_milestones_wait_for_morning(raw):
    raw["cats"][0]["birth_date"] = "2026-06-12"  # 4 месяца — 2026-10-12
    cfg = parse_config(raw)
    store = Store(":memory:")
    bot = CatBot(cfg, store)
    clock = Clock(cfg.timezone)
    bot.now = lambda: clock.value
    e = SimpleNamespace(bot=bot, store=store, clock=clock, app=SimpleNamespace(bot=FakeBot()))
    e.fake = e.app.bot
    store.bind_member("papa", 1, None, clock.set(0, 0))
    tick(e, 0, 1)
    assert not any("4 месяца" in m.text for m in e.fake.sent)
    tick(e, 9, 0)
    assert len([m for m in e.fake.sent if "4 месяца" in m.text]) == 1
    tick(e, 9, 1)
    assert len([m for m in e.fake.sent if "4 месяца" in m.text]) == 1


def test_weight_jump_needs_confirmation(env):
    env.clock.set(10, 0)
    msg = run_cmd(env, "tasya", env.bot.cmd_weight, ["Младшая", "15"])
    assert "Точно" in msg.replies[0]
    assert env.store.latest_weight("kitten") is None
    q = press(env, "tasya", "wconf|kitten|15000")
    assert env.store.latest_weight("kitten") == 15.0
    msg = run_cmd(env, "tasya", env.bot.cmd_weight, ["Младшая", "1,55"])
    assert "Точно" in msg.replies[0]  # 15 → 1,55 тоже резкий скачок
    run_cmd(env, "papa", env.bot.cmd_weight, ["Старшая", "4.1"])
    assert env.store.latest_weight("adult") == 4.1


def test_weight_child_entry_notifies_parents(env):
    env.clock.set(10, 0)
    run_cmd(env, "tasya", env.bot.cmd_weight, ["Младшая", "1.6"])
    assert env.store.latest_weight("kitten") == 1.6
    assert any("записал(а) вес" in m.text for m in to(env, "papa"))


# --- регистрация и группы ---


def test_register_requires_code(env):
    env.store.unbind_chat(CHATS["tasya"])
    q = press(env, "tasya", "reg|papa")
    assert "семейный код" in q.answers[0]
    assert env.store.chat_of("papa") == CHATS["papa"]
    q = press(env, "tasya", "reg|tasya", user_data={"code_ok": True})
    assert env.store.chat_of("tasya") == CHATS["tasya"]


def test_group_chat_callbacks_rejected(env):
    q_data = f"done|{lunch_id()}"
    from test_bot_flow import Query

    q = Query(q_data, CHATS["tanya"])
    update = SimpleNamespace(callback_query=q, effective_chat=SimpleNamespace(id=CHATS["tanya"], type="group"),
                             effective_user=SimpleNamespace(id=2, username=None))
    asyncio.run(env.bot.on_callback(update, SimpleNamespace(application=env.app, user_data={})))
    assert "личных" in q.answers[0]


# --- конфиг ---


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda r: r["tasks"][0].update(repeat_every_min="5"), "целое число без кавычек"),
        (lambda r: r["tasks"][0].update(give_up_after_min=10, escalate_after_min=20), "меньше give_up"),
        (lambda r: r["reminders"].update(max_repeats=True), "целое число"),
        (lambda r: r["tasks"][0].update(followup="yes"), "true или false"),
        (lambda r: r["tasks"][0].update(escalte_after_min=10), "escalate_after_min"),
        (lambda r: r["tasks"][0].update(holidays=["2026-11-04"]), "выше первого"),
        (lambda r: r["foods"][0].update(kcal_per_100g=4000), "kcal_per_kg"),
        (lambda r: r["foods"][0].update(kind="mokry"), "dry"),
        (lambda r: r["cats"][0].update(weight_kg=150), "15"),
        (lambda r: r.update(energy={"kitten_spay_cut": 10}), "доля"),
        (lambda r: r["members"][0].update(telegram_id="abc"), "целое число"),
    ],
)
def test_config_strict_validation(raw, mutate, message):
    mutate(raw)
    with pytest.raises(ConfigError, match=message):
        parse_config(raw)


def test_config_lenient_forms(raw):
    raw["members"][0]["telegram_id"] = "123456"
    raw["tasks"][0]["steps"] = "Один шаг"
    raw["foods"][1] = {"id": "adry", "name": "сухой", "kcal_per_kg": 3600}
    cfg = parse_config(raw)
    assert cfg.members[0].telegram_id == 123456
    assert cfg.task("breakfast").steps == ("Один шаг",)
    assert cfg.food("adry").kcal_per_100g == 360


def test_toml_syntax_error_is_config_error(tmp_path):
    from catbot.config import load_config

    p = tmp_path / "config.toml"
    p.write_text('join_code = "abcd"\nweight_kg = 1,5\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="через точку"):
        load_config(p)
