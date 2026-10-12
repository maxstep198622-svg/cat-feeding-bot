"""Чистая логика расписания: какие задачи бывают в какой день и что пора сделать.

Модуль не знает о Telegram и базе данных, поэтому полностью покрывается тестами.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from .config import WEEKDAYS, Config, Task


def day_mode(cfg: Config, day: date, override: str | None = None) -> str:
    """Режим дня: ручная отметка > период из конфига > праздник > сб/вс > будни."""
    if override and override in cfg.modes:
        return override
    for p in cfg.periods:
        if p.start <= day <= p.end:
            return p.mode
    if day in cfg.holidays or day.weekday() >= 5:
        return "weekend"
    return "weekday"


def day_tags(cfg: Config, day: date, override: str | None = None) -> tuple[str, str]:
    return WEEKDAYS[day.weekday()], day_mode(cfg, day, override)


def task_runs_on(task: Task, tags: tuple[str, str]) -> bool:
    return "all" in task.days or any(tag in task.days for tag in tags)


def task_time(task: Task, tags: tuple[str, str]) -> time:
    weekday, mode = tags
    if weekday in task.time_by_tag:
        return task.time_by_tag[weekday]
    if mode in task.time_by_tag:
        return task.time_by_tag[mode]
    return task.time


@dataclass(frozen=True)
class Instance:
    """Задача на конкретную дату."""

    id: str
    task: Task
    day: date
    due: datetime


def instance_id(day: date, task_id: str) -> str:
    return f"{day.isoformat()}|{task_id}"


def parse_instance_id(value: str) -> tuple[date, str]:
    day, task_id = value.split("|", 1)
    return date.fromisoformat(day), task_id


def instances_for_day(cfg: Config, day: date, override: str | None = None) -> list[Instance]:
    tags = day_tags(cfg, day, override)
    result = []
    for task in cfg.tasks:
        if not task_runs_on(task, tags):
            continue
        due = datetime.combine(day, task_time(task, tags), tzinfo=cfg.timezone)
        result.append(Instance(instance_id(day, task.id), task, day, due))
    result.sort(key=lambda i: (i.due, i.task.id))
    return result


# --- Состояние и решения -------------------------------------------------------


@dataclass
class State:
    status: str = "pending"  # pending | done | missed | skipped (сменили режим дня)
    assignee: str = ""
    sent_count: int = 0
    last_sent_at: datetime | None = None
    snoozed_until: datetime | None = None
    escalated_at: datetime | None = None
    done_at: datetime | None = None
    followup_sent_at: datetime | None = None


@dataclass(frozen=True)
class Action:
    kind: str  # remind | escalate | miss | followup | followup_skip
    instance: Instance
    repeat: int = 0  # номер повтора для remind (0 — первое напоминание)


def _escalate_after(cfg: Config, task: Task) -> timedelta:
    return timedelta(minutes=task.escalate_after_min or cfg.reminders.escalate_after_min)


def _give_up_after(cfg: Config, task: Task) -> timedelta:
    return timedelta(minutes=task.give_up_after_min or cfg.reminders.give_up_after_min)


FOLLOWUP_STALE = timedelta(minutes=90)


def _followup(cfg: Config, inst: Instance, st: State, now: datetime) -> list[Action]:
    """Через followup_min после «Готово» спросить, убраны ли остатки и всё ли съедено."""
    if not inst.task.followup or st.done_at is None or st.followup_sent_at is not None:
        return []
    at = st.done_at + timedelta(minutes=cfg.reminders.followup_min)
    if now < at:
        return []
    if now - at > FOLLOWUP_STALE:  # бот был выключен — старый вопрос уже не к месту
        return [Action("followup_skip", inst)]
    return [Action("followup", inst)]


def decide(cfg: Config, inst: Instance, st: State, now: datetime) -> list[Action]:
    """Что нужно сделать с задачей прямо сейчас. Вызывается на каждом «тике»."""
    if st.status == "done":
        return _followup(cfg, inst, st, now)
    if st.status != "pending":
        return []
    if now < inst.due:
        # Напоминание могло прийти раньше времени по кнопке «Я дома» — тогда работает только отсрочка.
        if st.sent_count and st.snoozed_until is not None and now >= st.snoozed_until:
            if st.last_sent_at is None or st.last_sent_at < st.snoozed_until:
                return [Action("remind", inst, st.sent_count)]
        return []
    late = now - inst.due
    if late >= _give_up_after(cfg, inst.task):
        return [Action("miss", inst)]

    actions: list[Action] = []
    task = inst.task
    repeat_every = task.repeat_every_min if task.repeat_every_min is not None else cfg.reminders.repeat_every_min
    max_repeats = task.max_repeats if task.max_repeats is not None else cfg.reminders.max_repeats
    if st.sent_count == 0:
        actions.append(Action("remind", inst, 0))
    elif st.snoozed_until is not None:
        if now >= st.snoozed_until and (st.last_sent_at is None or st.last_sent_at < st.snoozed_until):
            actions.append(Action("remind", inst, st.sent_count))
    elif (
        st.sent_count <= max_repeats
        and st.last_sent_at is not None
        and now - st.last_sent_at >= timedelta(minutes=repeat_every)
    ):
        actions.append(Action("remind", inst, st.sent_count))

    if st.escalated_at is None and late >= _escalate_after(cfg, inst.task):
        actions.append(Action("escalate", inst))
    return actions


def home_pull(cfg: Config, inst: Instance, st: State, member_id: str, now: datetime) -> bool:
    """Нажали «Я дома»: прислать ли эту задачу сразу, не дожидаясь её времени."""
    return (
        inst.task.on_home
        and st.status == "pending"
        and st.assignee == member_id
        and st.sent_count == 0
        and inst.due - now <= timedelta(minutes=cfg.reminders.home_window_min)
    )


def handoff_chain(cfg: Config, task: Task) -> list[str]:
    """Порядок, в котором задача передаётся дальше: основной, запасной, остальные родители."""
    chain = [task.primary]
    if task.backup and task.backup not in chain:
        chain.append(task.backup)
    for m in cfg.parents:
        if m.id not in chain:
            chain.append(m.id)
    return chain


def next_assignee(cfg: Config, task: Task, current: str) -> str | None:
    chain = handoff_chain(cfg, task)
    if current in chain:
        rest = chain[chain.index(current) + 1 :]
    else:
        rest = chain
    return rest[0] if rest else None


def share_totals(cfg: Config, day: date, override: str | None = None) -> dict[str, float]:
    """Сумма долей суточной нормы по каждой кошке за день — должна быть около 1,0."""
    totals = {c.id: 0.0 for c in cfg.cats}
    for inst in instances_for_day(cfg, day, override):
        for p in inst.task.portions:
            totals[p.cat] += p.share
    return totals


def check_shares(cfg: Config, tolerance: float = 0.02) -> list[str]:
    """Предупреждения, если в каком-то режиме дня доли не складываются в 100%."""
    warnings = []
    # Неделя без праздников и периодов покрывает все дни недели в обычных режимах,
    # плюс по одному дню в каждом дополнительном режиме.
    start = date(2001, 1, 1)  # понедельник
    probes = [(start + timedelta(days=i), None) for i in range(7)]
    probes += [(start, mode) for mode in cfg.modes if mode not in ("weekday", "weekend")]
    for day, override in probes:
        tags = day_tags(cfg, day, override)
        for cat_id, total in share_totals(cfg, day, override).items():
            if abs(total - 1.0) > tolerance:
                label = f"{tags[0]}/{tags[1]}"
                warnings.append(
                    f"{label}: у кошки {cat_id!r} доли за день дают {total * 100:.0f}% вместо 100%"
                )
    return warnings
