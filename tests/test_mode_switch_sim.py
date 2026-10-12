"""Симуляция дня семьи на реальном config.example.toml со сменой режима в разные моменты.

Инварианты: один приём не дают дважды; ни один приём нового плана не пропадает молча
(либо сделан, либо честно «пропущено»); нет ложных «пропущено» для данного приёма."""

import asyncio
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from catbot.bot import CatBot
from catbot.config import load_config
from catbot.scheduler import day_mode, instances_for_day
from catbot.storage import Store
from test_bot_flow import FakeBot, Query

CFG = load_config(Path(__file__).resolve().parents[1] / "config.example.toml")
CHATS = {"papa": 1, "tanya": 2, "tasya": 3}
MON = date(2026, 10, 12)
SAT = date(2026, 10, 17)


def make():
    store = Store(":memory:")
    bot = CatBot(CFG, store)
    now = {"t": None}
    bot.now = lambda: now["t"]
    app = SimpleNamespace(bot=FakeBot())
    for m, c in CHATS.items():
        store.bind_member(m, c, None, datetime(2026, 10, 1, tzinfo=CFG.timezone))
    return bot, store, app, now


def press(bot, app, member, data):
    q = Query(data, CHATS[member])
    update = SimpleNamespace(
        callback_query=q,
        effective_chat=SimpleNamespace(id=CHATS[member], type="private"),
        effective_user=SimpleNamespace(id=CHATS[member], username=None),
    )
    asyncio.run(bot.on_callback(update, SimpleNamespace(application=app, user_data={})))
    return q


def simulate(day, switches, delay=3, home_at=None, responsive=True):
    """switches: [(hh, mm, target_day_offset, mode)]; семья жмёт «Готово» через delay минут после напоминания."""
    bot, store, app, now = make()
    t = datetime(day.year, day.month, day.day, 0, 0, tzinfo=CFG.timezone)
    end = t + timedelta(days=1, hours=3)
    switches = sorted(switches)
    while t < end:
        now["t"] = t
        for sw in [s for s in switches if (s[0], s[1]) == (t.hour, t.minute) and t.date() == day]:
            target = day + timedelta(days=sw[2])
            press(bot, app, "tanya", f"mode|{target}:{sw[3]}")
        if home_at and (t.hour, t.minute) == home_at and t.date() == day:
            from test_bot_flow import Message

            msg = Message("🏠 Я дома", CHATS["tasya"])
            upd = SimpleNamespace(effective_message=msg, effective_chat=SimpleNamespace(id=3, type="private"),
                                  effective_user=SimpleNamespace(id=3, username=None))
            asyncio.run(bot.on_text(upd, SimpleNamespace(application=app, user_data={}, args=[])))
        asyncio.run(bot.run_tick(app, t))
        if responsive:
            for r in store.for_day(day):
                if r.status == "pending" and r.sent_count > 0 and t - r.last_sent_at >= timedelta(minutes=delay):
                    press(bot, app, r.assignee, f"done|{r.id}")
        t += timedelta(minutes=1)
    return store, app


def check_invariants(store, day):
    recs = store.for_day(day)
    done = defaultdict(list)
    missed = defaultdict(list)
    for r in recs:
        task = CFG.task(r.task_id)
        if not task.portions:
            continue
        if r.status == "done":
            done[task.meal].append(r.task_id)
        elif r.status == "missed":
            missed[task.meal].append(r.task_id)
    doubles = {m: v for m, v in done.items() if len(v) > 1}
    assert not doubles, f"один приём дали дважды: {doubles}"
    false_missed = {m: v for m, v in missed.items() if m in done}
    assert not false_missed, f"ложное «пропущено» при данном приёме: {false_missed}"
    final_meals = {i.task.meal for i in instances_for_day(CFG, day, store.day_mode(day)) if i.task.portions}
    lost = [m for m in final_meals if m not in done and m not in missed]
    assert not lost, f"приём пропал молча: {lost}"
    return done


SWITCH_TIMES = [(6, 0), (7, 21), (7, 31), (7, 36), (8, 6), (9, 0), (12, 30), (13, 20), (13, 30), (14, 1),
                (14, 20), (17, 40), (18, 20), (22, 10)]


@pytest.mark.parametrize("hhmm", SWITCH_TIMES)
@pytest.mark.parametrize("mode", ["kanikuly", "papa_away", "weekend"])
def test_weekday_single_switch(hhmm, mode):
    store, _ = simulate(MON, [(hhmm[0], hhmm[1], 0, mode)])
    check_invariants(store, MON)


@pytest.mark.parametrize("hhmm", [(7, 45), (8, 3), (13, 20), (17, 40)])
def test_saturday_switch_to_weekday(hhmm):
    store, _ = simulate(SAT, [(hhmm[0], hhmm[1], 0, "weekday")])
    check_invariants(store, SAT)


@pytest.mark.parametrize("hhmm", [(7, 15), (7, 31), (13, 30), (17, 45)])
@pytest.mark.parametrize("mode", ["kanikuly", "papa_away", "weekend"])
def test_wrong_mode_then_undo(hhmm, mode):
    store, _ = simulate(MON, [(hhmm[0], hhmm[1], 0, mode), (hhmm[0], hhmm[1] + 1, 0, "auto")])
    check_invariants(store, MON)


def test_home_early_then_switch():
    store, _ = simulate(MON, [(13, 20, 0, "kanikuly")], home_at=(12, 0))
    check_invariants(store, MON)


def test_preset_tomorrow_then_revert():
    store, _ = simulate(MON - timedelta(days=1), [(20, 0, 1, "kanikuly")])
    store2, _ = simulate(MON, [(8, 0, 0, "auto")])
    check_invariants(store2, MON)


def test_no_switch_full_day_feeds_exactly_100_percent():
    store, app = simulate(MON, [])
    done = check_invariants(store, MON)
    shares = defaultdict(float)
    for r in store.for_day(MON):
        if r.status == "done":
            for p in CFG.task(r.task_id).portions:
                shares[p.cat] += p.share
    assert shares == pytest.approx({"faya": 1.0, "stefa": 1.0})
    assert not [m for m in app.bot.sent if "ещё не отмечено" in m.text]  # семья отзывчива — эскалаций нет
