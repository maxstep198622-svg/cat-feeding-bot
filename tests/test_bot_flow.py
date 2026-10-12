"""Сценарии бота на заглушке Telegram: напоминание → повтор → эскалация → «Готово»."""

import asyncio
from datetime import date, datetime
from itertools import count
from types import SimpleNamespace

import pytest

from catbot.bot import CatBot
from catbot.storage import Store

MON = date(2026, 10, 12)
CHATS = {"papa": 1, "tanya": 2, "tasya": 3}


class FakeBot:
    def __init__(self):
        self.sent = []
        self.edited = []
        self._ids = count(1)

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        msg = SimpleNamespace(message_id=next(self._ids), chat_id=chat_id, text=text, markup=reply_markup)
        self.sent.append(msg)
        return msg

    async def edit_message_text(self, text, chat_id, message_id, parse_mode=None):
        self.edited.append((chat_id, message_id, text))


class Clock:
    def __init__(self, tz):
        self.tz = tz
        self.value = None

    def set(self, hh, mm, day=MON):
        self.value = datetime(day.year, day.month, day.day, hh, mm, tzinfo=self.tz)
        return self.value


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


def tick(env, hh, mm, day=MON):
    asyncio.run(env.bot.run_tick(env.app, env.clock.set(hh, mm, day)))


def to(env, member):
    return [m for m in env.fake.sent if m.chat_id == CHATS[member]]


class Query:
    def __init__(self, data, chat_id):
        self.data = data
        self.answers = []
        self.message = SimpleNamespace(chat=SimpleNamespace(id=chat_id), message_id=999)
        self.edited_markup = False

    async def answer(self, text=None, show_alert=False):
        self.answers.append(text)

    async def edit_message_reply_markup(self, markup):
        self.edited_markup = True

    async def edit_message_text(self, text, parse_mode=None, reply_markup=None):
        self.answers.append(text)


def press(env, member, data):
    q = Query(data, CHATS[member])
    update = SimpleNamespace(
        callback_query=q,
        effective_chat=SimpleNamespace(id=CHATS[member]),
        effective_user=SimpleNamespace(id=CHATS[member] * 1000, username=None),
    )
    asyncio.run(env.bot.on_callback(update, SimpleNamespace(application=env.app, user_data={})))
    return q


def lunch_id():
    return f"{MON.isoformat()}|lunch"


def test_full_cycle_child_does_it(env):
    tick(env, 14, 0)
    assert to(env, "tasya") == []
    tick(env, 14, 30)
    first = to(env, "tasya")
    assert len(first) == 1 and "Тася, пора покормить кошек" in first[0].text
    assert "г —" in first[0].text  # граммы посчитаны
    assert "Закрой Младшую" in first[0].text

    tick(env, 14, 35)
    assert len(to(env, "tasya")) == 1
    tick(env, 14, 40)
    assert len(to(env, "tasya")) == 2 and "Напоминаю" in to(env, "tasya")[1].text

    q = press(env, "tasya", f"done|{lunch_id()}")
    assert q.answers and q.answers[0]  # похвала
    rec = env.store.get(lunch_id())
    assert rec.status == "done" and rec.done_by == "tasya"
    # родители получили отметку, сообщения-напоминания закрыты
    assert any("сделано: Тася" in m.text for m in to(env, "tanya"))
    assert {e[0] for e in env.fake.edited} == {CHATS["tasya"]}

    before = len(env.fake.sent)
    tick(env, 15, 10)
    assert len(env.fake.sent) == before  # больше ничего не шлём


def test_escalation_and_double_feed_guard(env):
    tick(env, 14, 30)
    tick(env, 14, 40)
    tick(env, 14, 50)
    tick(env, 15, 0)
    esc = [m for m in to(env, "tanya") if "ещё не отмечено" in m.text]
    assert len(esc) == 1 and "Тася" in esc[0].text
    assert any("ещё не отмечено" in m.text for m in to(env, "papa"))
    assert len(to(env, "tasya")) == 3  # 1 + 2 повтора, больше не спамим
    tick(env, 15, 20)
    assert len([m for m in to(env, "tanya") if "ещё не отмечено" in m.text]) == 1

    press(env, "tanya", f"done|{lunch_id()}")
    q = press(env, "tasya", f"done|{lunch_id()}")
    assert "Второй раз кормить не нужно" in q.answers[0]
    assert env.store.get(lunch_id()).done_by == "tanya"


def test_missed_notifies_parents(env):
    for hh, mm in [(14, 30), (14, 40), (14, 50), (15, 0), (17, 0)]:
        tick(env, hh, mm)
    assert env.store.get(lunch_id()).status == "missed"
    assert any("так и не отмечено" in m.text for m in to(env, "papa"))


def test_handoff(env):
    tick(env, 14, 30)
    q = press(env, "tasya", f"pass|{lunch_id()}")
    assert "Таня" in q.answers[0]
    rec = env.store.get(lunch_id())
    assert rec.assignee == "tanya" and rec.sent_count == 1
    assert any("не может сейчас" in m.text for m in to(env, "tanya"))


def test_snooze(env):
    tick(env, 14, 30)
    press(env, "tasya", f"snooze|{lunch_id()}")
    tick(env, 14, 35)
    assert len(to(env, "tasya")) == 1
    tick(env, 14, 40)
    assert len(to(env, "tasya")) == 2


def test_early_done_means_no_reminder(env):
    tick(env, 12, 0)
    press(env, "tasya", f"done|{lunch_id()}")
    tick(env, 14, 30)
    assert to(env, "tasya") == []


def test_unregistered_assignee_falls_back_to_parents(env):
    env.store.unbind_chat(CHATS["tasya"])
    tick(env, 14, 30)
    assert any("ещё не отмечено" in m.text for m in to(env, "tanya"))
    tick(env, 14, 40)
    assert len([m for m in to(env, "tanya") if "ещё не отмечено" in m.text]) == 1  # без спама на повторах


def test_pause(env):
    env.store.set_kv("paused", "1")
    tick(env, 14, 30)
    assert env.fake.sent == []


def test_yesterday_pending_closed(env):
    tick(env, 14, 30)
    tick(env, 8, 0, day=date(2026, 10, 13))
    assert env.store.get(lunch_id()).status == "missed"


def test_mode_switch_drops_unstarted_tasks(env):
    tick(env, 6, 0)
    assert [r.task_id for r in env.store.for_day(MON)] == ["breakfast", "lunch", "dinner"]
    press(env, "papa", f"mode|{MON.isoformat()}:weekend")
    assert [r.task_id for r in env.store.for_day(MON)] == ["wk_breakfast", "wk_lunch", "dinner"]


def test_child_cannot_change_mode(env):
    q = press(env, "tasya", f"mode|{MON.isoformat()}:weekend")
    assert "родители" in q.answers[0]
    assert env.store.day_mode(MON) is None


def test_daily_report_once(cfg, env):
    from dataclasses import replace
    from datetime import time

    env.bot.cfg = replace(cfg, reminders=replace(cfg.reminders, daily_report=time(21, 45)))
    tick(env, 21, 45)
    tick(env, 21, 50)
    reports = [m for m in to(env, "papa") if "Итоги дня" in m.text]
    assert len(reports) == 1
    assert not [m for m in to(env, "tasya") if "Итоги дня" in m.text]
