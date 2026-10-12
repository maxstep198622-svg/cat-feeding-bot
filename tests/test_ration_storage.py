from datetime import date, datetime

import pytest

from catbot.ration import daily_need, format_grams, portion_grams, rer
from catbot.scheduler import instances_for_day
from catbot.storage import Store

MON = date(2026, 10, 12)


def test_rer():
    assert rer(1) == pytest.approx(70)
    assert rer(4) == pytest.approx(70 * 4**0.75)


def test_stages(cfg):
    kitten, adult = cfg.cats
    n = daily_need(cfg, kitten, MON)
    assert n.stage == "котёнок до 4 месяцев" and n.factor == 2.5
    assert n.kcal == pytest.approx(rer(1.5) * 2.5)  # ≈ 237 ккал
    assert daily_need(cfg, kitten, date(2026, 12, 1)).factor == cfg.energy.kitten_4_6m
    assert daily_need(cfg, kitten, date(2027, 2, 1)).factor == cfg.energy.kitten_6_9m
    assert daily_need(cfg, kitten, date(2027, 5, 1)).factor == cfg.energy.kitten_9_12m
    assert daily_need(cfg, kitten, date(2027, 8, 1)).stage == "взрослая нестерилизованная"
    a = daily_need(cfg, adult, MON)
    assert a.stage == "взрослая стерилизованная"
    assert a.kcal == pytest.approx(rer(4) * cfg.energy.adult_neutered)


def test_kitten_spay_cut_after_recovery(raw):
    from catbot.config import parse_config

    raw["cats"][0].update(neutered=True, neutered_on="2026-11-20")
    cfg = parse_config(raw)
    kitten = cfg.cats[0]
    before = daily_need(cfg, kitten, date(2026, 11, 19))
    recovering = daily_need(cfg, kitten, date(2026, 11, 25))
    after = daily_need(cfg, kitten, date(2026, 12, 4))
    assert before.factor == recovering.factor == cfg.energy.kitten_4_6m
    assert "восстановление" in recovering.stage
    assert after.factor == pytest.approx(cfg.energy.kitten_4_6m * 0.9)


def test_adult_uses_ideal_weight(raw):
    from catbot.config import parse_config

    raw["cats"][1]["ideal_weight_kg"] = 3.6
    cfg = parse_config(raw)
    a = daily_need(cfg, cfg.cats[1], MON, weight_kg=4.4)
    assert a.weight_kg == 3.6 and a.kcal == pytest.approx(rer(3.6) * cfg.energy.adult_neutered)


def test_stage_boundaries_use_calendar_months(cfg):
    kitten = cfg.cats[0]  # родилась 2026-07-10
    assert daily_need(cfg, kitten, date(2026, 11, 9)).stage == "котёнок до 4 месяцев"
    assert daily_need(cfg, kitten, date(2026, 11, 10)).stage == "котёнок 4–6 месяцев"
    assert daily_need(cfg, kitten, date(2027, 7, 9)).stage.startswith("котёнок 9–12")
    assert daily_need(cfg, kitten, date(2027, 7, 10)).stage == "взрослая нестерилизованная"


def test_milestones(cfg):
    from catbot.ration import add_months, due_milestones

    kitten = cfg.cats[0]  # родилась 2026-07-10
    assert add_months(date(2026, 8, 31), 6) == date(2027, 2, 28)
    assert due_milestones(kitten, date(2026, 11, 9)) == []
    assert due_milestones(kitten, date(2026, 11, 10)) == [("4m", date(2026, 11, 10))]
    assert due_milestones(kitten, date(2026, 11, 16)) == [("4m", date(2026, 11, 10))]
    assert due_milestones(kitten, date(2026, 11, 17)) == []


def test_manual_kcal_and_weight_override(raw):
    from catbot.config import parse_config

    raw["cats"][1]["daily_kcal"] = 200
    cfg = parse_config(raw)
    n = daily_need(cfg, cfg.cats[1], MON)
    assert n.manual and n.kcal == 200
    heavier = daily_need(cfg, cfg.cats[0], MON, weight_kg=2.0)
    assert heavier.weight_kg == 2.0 and heavier.kcal == pytest.approx(rer(2.0) * cfg.energy.kitten_0_4m)


def test_portion_grams(cfg):
    need = daily_need(cfg, cfg.cats[0], MON)
    p = cfg.task("breakfast").portions[0]
    assert portion_grams(cfg, p, need) == pytest.approx(need.kcal * 0.3 / 4.0)
    assert format_grams(7.25) == "7,2 г" or format_grams(7.25) == "7,3 г"
    assert format_grams(23.6) == "24 г"


def now(cfg, hh=12, mm=0):
    return datetime(2026, 10, 12, hh, mm, tzinfo=cfg.timezone)


def test_store_instances(cfg):
    s = Store(":memory:")
    insts = instances_for_day(cfg, MON)
    s.ensure_instances(insts)
    s.ensure_instances(insts)  # повторный вызов не дублирует
    recs = s.for_day(MON)
    assert [r.task_id for r in recs] == ["breakfast", "lunch", "dinner"]
    assert recs[1].assignee == "tasya" and recs[1].status == "pending"

    lunch = insts[1].id
    assert s.mark_done(lunch, "tasya", now(cfg, 14, 35))
    assert not s.mark_done(lunch, "tanya", now(cfg, 14, 40))  # второй раз — нельзя
    rec = s.get(lunch)
    assert rec.done_by == "tasya" and rec.done_at == now(cfg, 14, 35)
    assert not s.mark_missed(lunch)


def test_plan_sync_rules(cfg):
    from catbot.scheduler import Existing, plan_sync

    tz = cfg.timezone
    at = lambda hh, mm: datetime(2026, 10, 12, hh, mm, tzinfo=tz)
    weekend = instances_for_day(cfg, MON, "weekend")  # 08:30 завтрак, 13:30 обед, 19:00 ужин
    ids = {i.task.id: i.id for i in weekend}

    # Первый запуск в день — всё по расписанию, даже прошедшее
    p = plan_sync(weekend, [], at(15, 0))
    assert [i.task.id for i in p.create] == ["wk_breakfast", "wk_lunch", "dinner"]

    # Будний завтрак был в 07:45, обед (14:30) ещё не наступил; в 14:00 включили выходной
    recs = [
        Existing(f"{MON}|breakfast", at(7, 45), "done", 1, True),
        Existing(f"{MON}|lunch", at(14, 30), "pending", 0, True),
        Existing(f"{MON}|dinner", at(19, 0), "pending", 0, True),
    ]
    p = plan_sync(weekend, recs, at(14, 0))
    assert p.delete == [f"{MON}|lunch"]
    assert [i.task.id for i in p.create_skipped] == ["wk_breakfast"]  # завтрак уже был
    assert [i.task.id for i in p.create_now] == ["wk_lunch"]  # обеда не было — напомнить сейчас
    assert p.create_now[0].due == at(14, 0)

    # Обед уже напомнили (идёт), режим сменили — обед доводим до конца, выходной обед не нужен
    recs[1] = Existing(f"{MON}|lunch", at(14, 30), "pending", 1, True)
    p = plan_sync(weekend, recs, at(14, 35))
    assert f"{MON}|lunch" not in p.delete + p.cancel
    assert p.create_now == [] and {i.task.id for i in p.create_skipped} == {"wk_breakfast", "wk_lunch"}

    # Вернули режим обратно: отменённая (но начатая) задача оживает, пустые следы удаляются
    weekday = instances_for_day(cfg, MON)
    recs = [
        Existing(f"{MON}|lunch", at(14, 30), "skipped", 1, True),
        Existing(ids["wk_lunch"], at(13, 30), "skipped", 0, True),
    ]
    p = plan_sync(weekday, recs, at(14, 40))
    assert p.revive == [f"{MON}|lunch"] and p.delete == [ids["wk_lunch"]]

    # Задачу убрали из конфига — отменяем, даже если её время прошло
    recs = [Existing(f"{MON}|gone", at(9, 0), "pending", 1, True, known=False)]
    assert plan_sync(weekday, recs, at(10, 0)).cancel == [f"{MON}|gone"]


def test_store_members(cfg):
    s = Store(":memory:")
    assert s.bind_member("tasya", 100, "t", now(cfg)) is None
    assert s.chat_of("tasya") == 100 and s.member_by_chat(100) == "tasya"
    # тот же чат перепривязали к другому участнику
    assert s.bind_member("papa", 100, "t", now(cfg)) == "tasya"
    assert s.chat_of("tasya") is None and s.chat_of("papa") == 100
    # участник сменил аккаунт
    s.bind_member("papa", 200, "p", now(cfg))
    assert s.member_by_chat(100) is None and s.chat_of("papa") == 200


def test_store_reassign_resets_counters(cfg):
    s = Store(":memory:")
    insts = instances_for_day(cfg, MON)
    s.ensure_instances(insts)
    iid = insts[1].id
    s.mark_sent(iid, now(cfg, 14, 30))
    s.snooze(iid, now(cfg, 14, 40))
    s.reassign(iid, "tanya")
    rec = s.get(iid)
    assert (rec.assignee, rec.sent_count, rec.last_sent_at, rec.snoozed_until) == ("tanya", 0, None, None)


def test_weights_and_kv(cfg):
    s = Store(":memory:")
    s.add_weight("kitten", date(2026, 10, 5), 1.4, "papa")
    s.add_weight("kitten", date(2026, 10, 12), 1.5, "tasya")
    assert s.latest_weight("kitten") == 1.5
    assert s.weights("kitten") == [(date(2026, 10, 12), 1.5), (date(2026, 10, 5), 1.4)]
    s.set_day_mode(MON, "weekend")
    assert s.day_mode(MON) == "weekend"
    s.set_day_mode(MON, None)
    assert s.day_mode(MON) is None
    s.set_kv("paused", "1")
    assert s.get_kv("paused") == "1"
    s.set_kv("paused", None)
    assert s.get_kv("paused") is None
