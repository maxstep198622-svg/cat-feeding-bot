from datetime import date, datetime, timedelta

import pytest

from catbot.config import ConfigError, parse_config
from catbot.scheduler import (
    State,
    check_shares,
    day_mode,
    decide,
    instances_for_day,
    next_assignee,
    parse_instance_id,
)

MON = date(2026, 10, 12)
TUE = date(2026, 10, 13)
SAT = date(2026, 10, 17)
SUN = date(2026, 10, 18)


def at(cfg, day, hh, mm):
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=cfg.timezone)


def ids(insts):
    return [i.task.id for i in insts]


def test_day_modes(cfg):
    assert day_mode(cfg, MON) == "weekday"
    assert day_mode(cfg, SAT) == "weekend"
    assert day_mode(cfg, date(2026, 11, 4)) == "weekend"  # праздник в среду
    assert day_mode(cfg, date(2027, 1, 5)) == "holiday"  # период каникул
    assert day_mode(cfg, MON, override="weekend") == "weekend"
    assert day_mode(cfg, MON, override="unknown") == "weekday"


def test_instances_per_day(cfg):
    assert ids(instances_for_day(cfg, MON)) == ["breakfast", "lunch", "dinner"]
    assert ids(instances_for_day(cfg, SAT)) == ["wk_breakfast", "wk_lunch", "dinner"]
    assert ids(instances_for_day(cfg, SUN)) == ["wk_breakfast", "weigh", "wk_lunch", "dinner"]
    assert ids(instances_for_day(cfg, date(2027, 1, 5))) == ["wk_breakfast", "wk_lunch", "dinner"]


def test_time_by_day(cfg):
    mon = {i.task.id: i.due for i in instances_for_day(cfg, MON)}
    tue = {i.task.id: i.due for i in instances_for_day(cfg, TUE)}
    assert mon["lunch"] == at(cfg, MON, 14, 30)
    assert tue["lunch"] == at(cfg, TUE, 15, 30)


def test_instance_id_roundtrip(cfg):
    inst = instances_for_day(cfg, MON)[0]
    assert parse_instance_id(inst.id) == (MON, "breakfast")


def lunch(cfg):
    return next(i for i in instances_for_day(cfg, MON) if i.task.id == "lunch")


def kinds(actions):
    return [a.kind for a in actions]


def test_decide_flow(cfg):
    inst = lunch(cfg)
    st = State(assignee="tasya")
    assert decide(cfg, inst, st, at(cfg, MON, 14, 29)) == []
    first = decide(cfg, inst, st, at(cfg, MON, 14, 30))
    assert kinds(first) == ["remind"] and first[0].repeat == 0

    st.sent_count, st.last_sent_at = 1, at(cfg, MON, 14, 30)
    assert decide(cfg, inst, st, at(cfg, MON, 14, 39)) == []
    rep = decide(cfg, inst, st, at(cfg, MON, 14, 40))
    assert kinds(rep) == ["remind"] and rep[0].repeat == 1

    st.sent_count, st.last_sent_at = 3, at(cfg, MON, 14, 50)  # исчерпали повторы
    assert decide(cfg, inst, st, at(cfg, MON, 14, 59)) == []
    assert kinds(decide(cfg, inst, st, at(cfg, MON, 15, 0))) == ["escalate"]

    st.escalated_at = at(cfg, MON, 15, 0)
    assert decide(cfg, inst, st, at(cfg, MON, 16, 0)) == []
    assert kinds(decide(cfg, inst, st, at(cfg, MON, 17, 0))) == ["miss"]


def test_decide_snooze(cfg):
    inst = lunch(cfg)
    st = State(assignee="tasya", sent_count=1, last_sent_at=at(cfg, MON, 14, 30), snoozed_until=at(cfg, MON, 14, 45))
    assert decide(cfg, inst, st, at(cfg, MON, 14, 40)) == []  # обычный повтор подавлен отсрочкой
    assert kinds(decide(cfg, inst, st, at(cfg, MON, 14, 45))) == ["remind"]


def test_decide_done_is_silent(cfg):
    inst = lunch(cfg)
    st = State(status="done", assignee="tasya")
    assert decide(cfg, inst, st, at(cfg, MON, 18, 0)) == []


def test_decide_late_start_after_restart(cfg):
    """Бот перезапустился на 40-й минуте: сразу и напоминание, и эскалация."""
    inst = lunch(cfg)
    assert kinds(decide(cfg, inst, State(assignee="tasya"), at(cfg, MON, 15, 10))) == ["remind", "escalate"]


def test_handoff_chain(cfg):
    task = cfg.task("lunch")
    assert next_assignee(cfg, task, "tasya") == "tanya"
    assert next_assignee(cfg, task, "tanya") == "papa"
    assert next_assignee(cfg, task, "papa") is None


def test_share_check(cfg, raw):
    assert check_shares(cfg) == []
    raw["tasks"][0]["portions"][0]["share"] = 0.5
    warnings = check_shares(parse_config(raw))
    assert warnings and all("kitten" in w for w in warnings)


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda r: r["tasks"][0].update(primary="nobody"), "нет члена семьи"),
        (lambda r: r["tasks"][0].update(time="7.45"), "ЧЧ:ММ"),
        (lambda r: r["tasks"][0].update(days=["someday"]), "неизвестный день"),
        (lambda r: r["tasks"][0]["portions"][0].update(share=1.5), "share"),
        (lambda r: r["tasks"][0]["portions"][0].update(cat="dog"), "нет кошки"),
        (lambda r: r.update(join_code="12"), "join_code"),
        (lambda r: r["reminders"].update(escalate_after_min=200), "escalate_after_min"),
        (lambda r: r["members"].__setitem__(slice(None), [{"id": "x", "name": "X", "role": "child"}]), "родитель"),
    ],
)
def test_config_errors(raw, mutate, message):
    mutate(raw)
    with pytest.raises(ConfigError, match=message):
        parse_config(raw)


def test_due_is_timezone_aware(cfg):
    inst = lunch(cfg)
    assert inst.due.utcoffset() == timedelta(hours=3)
