"""Тексты сообщений. Пишем коротко и понятно ребёнку."""

from __future__ import annotations

from datetime import date, datetime
from html import escape

from .config import Cat, Config, Task
from .ration import Need, format_grams, portion_grams
from .scheduler import Instance
from .storage import Record

KIND_ICON = {"feeding": "🍽", "water": "💧", "care": "🧽"}
KIND_CALL = {
    "feeding": "пора покормить кошек!",
    "water": "пора поменять кошкам воду!",
    "care": "есть дело по уходу за кошками.",
}
WEEKDAY_SHORT = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
PRAISE = (
    "Молодец! 🐾",
    "Спасибо! Кошки сыты и довольны 😺",
    "Отлично! Так держать 💪",
    "Супер! Мурлыкают от благодарности 💛",
    "Готово! Ты настоящий кошачий повар 👩‍🍳",
)


def num(value: float, digits: int | None = None) -> str:
    text = f"{value:.{digits}f}" if digits is not None else f"{value:g}"
    return text.replace(".", ",")


def hm(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def day_title(cfg: Config, day: date, mode: str) -> str:
    return f"{WEEKDAY_SHORT[day.weekday()]} {day.strftime('%d.%m')} — {escape(cfg.mode_titles.get(mode, mode))}"


def portion_lines(cfg: Config, task: Task, needs: dict[str, Need]) -> list[str]:
    lines = []
    for p in task.portions:
        need = needs[p.cat]
        cat = need.cat
        food = cfg.food(p.food)
        g = portion_grams(cfg, p, need)
        lines.append(f"{cat.emoji} <b>{escape(cat.name)}</b>: {format_grams(g)} — {escape(food.name)}")
    return lines


def task_body(cfg: Config, task: Task, needs: dict[str, Need]) -> str:
    parts = []
    portions = portion_lines(cfg, task, needs)
    if portions:
        parts.append("\n".join(portions))
    if task.water:
        parts.append(f"💧 {escape(task.water)}")
    if task.steps:
        steps = "\n".join(f"{n}. {escape(s)}" for n, s in enumerate(task.steps, 1))
        parts.append(f"<b>Как делать:</b>\n{steps}")
    return "\n\n".join(parts)


def reminder(cfg: Config, inst: Instance, who: str, needs: dict[str, Need], repeat: int) -> str:
    task = inst.task
    head = f"{KIND_ICON[task.kind]} <b>{escape(task.name)}</b> · {hm(inst.due)}"
    if repeat:
        call = f"🔔 Напоминаю ещё раз: {escape(who)}, {KIND_CALL[task.kind]}"
    else:
        call = f"{escape(who)}, {KIND_CALL[task.kind]}"
    body = task_body(cfg, task, needs)
    if task.followup:
        tail = "Как только поставишь миски — нажми «✅ Готово». Через полчаса напомню убрать остатки."
    else:
        tail = "Когда сделаешь — нажми «✅ Готово»."
    if any("Готово" in step for step in task.steps):
        tail = ""  # шаги уже говорят, когда нажать «Готово»
    parts = [head, call]
    if body:
        parts += ["", body]
    if tail:
        parts += ["", tail]
    return "\n".join(parts)


def escalation(
    cfg: Config, inst: Instance, assignee: str, late_min: int, needs: dict[str, Need], unreachable: bool = False
) -> str:
    task = inst.task
    head = f"⚠️ <b>{escape(task.name)}</b> ({hm(inst.due)}) ещё не отмечено."
    if unreachable:
        info = f"Ответственный ({escape(assignee)}) не подключён к боту — напоминание не дошло."
    else:
        info = f"Ответственный: {escape(assignee)}. Прошло {late_min} мин."
    hint = "Если ты дома — сделай сам и нажми «✅ Готово». Если нет — позвони и напомни."
    body = task_body(cfg, task, needs)
    return "\n".join([head, info, hint, "", body]) if body else "\n".join([head, info, hint])


def handed_off(cfg: Config, inst: Instance, from_name: str, to_name: str, needs: dict[str, Need]) -> str:
    task = inst.task
    head = f"🔁 {escape(from_name)} не может сейчас: <b>{escape(task.name)}</b> ({hm(inst.due)})"
    call = f"{escape(to_name)}, сделаешь? Как сделаешь — нажми «✅ Готово»."
    body = task_body(cfg, task, needs)
    return "\n".join([head, call, "", body]) if body else "\n".join([head, call])


def done_stamp(task: Task, due: datetime, who: str, at: datetime) -> str:
    return f"✅ <b>{escape(task.name)}</b> ({hm(due)}) — сделано: {escape(who)}, {hm(at)}"


def already_done(who: str, at: datetime, kind: str, day: date | None = None, today: date | None = None) -> str:
    when = hm(at) if day is None or day == today else f"{day.strftime('%d.%m')} в {hm(at)}"
    if kind == "feeding":
        if day is not None and day != today:
            return f"Это кнопка за {day.strftime('%d.%m')}: тогда покормил(а) {who} в {hm(at)}."
        return f"Уже покормили: {who}, {when}. Второй раз кормить не нужно 🙂"
    return f"Уже сделано: {who}, {when}."


def missed(task: Task, due: datetime, assignee: str) -> str:
    return (
        f"❌ <b>{escape(task.name)}</b> ({hm(due)}) так и не отмечено (ответственный: {escape(assignee)}). "
        "Если на самом деле всё сделано — нажмите кнопку ниже."
    )


def followup(inst: Instance, who: str) -> str:
    return (
        f"⏱ <b>{escape(inst.task.name)}</b>: прошло полчаса.\n"
        f"{escape(who)}, убери миски с едой (влажный корм — выбросить), потом открой дверь.\n"
        "Всё съели?"
    )


def leftovers_alert(inst: Instance, who: str, twice: bool) -> str:
    text = f"🍽 {escape(inst.task.name)} ({hm(inst.due)}): осталось много корма (отметил(а) {escape(who)})."
    if twice:
        text += (
            "\n⚠️ Второе кормление подряд. Посмотрите, всё ли в порядке: если котёнок вялый, не играет, "
            "есть рвота или понос — к ветеринару в тот же день."
        )
    return text


SYMPTOMS = {
    "lethargy": "вялая, не играет",
    "noeat": "не ест",
    "vomit": "рвота",
    "diarrhea": "понос",
    "blood": "кровь",
    "litter": "тужится в лотке / не может пописать",
    "belly": "краснота или корочки на животе у Старшей",
    "other": "другое — позвоню",
}


def alert(who: str, cat_name: str, symptom: str, at: datetime) -> str:
    return (
        f"🚨 <b>{escape(who)}</b>: {escape(cat_name)} — {escape(symptom)} ({hm(at)}).\n"
        "Позвоните и посмотрите сами. Срочно к врачу: вялость у котёнка, кровь, повторная рвота, "
        "не может пописать."
    )


STATUS_ICON = {"done": "✅", "missed": "❌"}


def day_summary(cfg: Config, day: date, mode: str, records: list[Record], now: datetime) -> str:
    lines = [f"📅 <b>{day_title(cfg, day, mode)}</b>"]
    if not records:
        lines.append("На этот день задач нет.")
    for r in records:
        try:
            task = cfg.task(r.task_id)
        except KeyError:
            continue
        name = escape(task.name)
        assignee = escape(_name(cfg, r.assignee))
        if r.status == "done":
            lines.append(f"✅ {hm(r.due_at)} {name} — {escape(_name(cfg, r.done_by))}, {hm(r.done_at)}")
        elif r.status == "missed":
            lines.append(f"❌ {hm(r.due_at)} {name} — пропущено ({assignee})")
        elif r.status == "skipped":
            lines.append(f"⏭ {hm(r.due_at)} {name} — не нужно (сменили режим)")
        elif now < r.due_at:
            lines.append(f"⏳ {hm(r.due_at)} {name} — {assignee}")
        else:
            lines.append(f"🔔 {hm(r.due_at)} {name} — ждём ({assignee})")
    return "\n".join(lines)


def ration_text(cfg: Config, needs: dict[str, Need], weighed: dict[str, date | None], plans: dict[str, list[Instance]]) -> str:
    blocks = []
    for need in needs.values():
        cat = need.cat
        if cat.ideal_weight_kg and need.weight_kg == cat.ideal_weight_kg and need.stage.startswith("взрослая"):
            w_note = "идеальный вес"
        elif weighed.get(cat.id):
            w_note = f"взвешена {weighed[cat.id].strftime('%d.%m')}"
        else:
            w_note = "вес из настроек"
        lines = [
            f"{cat.emoji} <b>{escape(cat.name)}</b> — {num(need.age_months, 1)} мес., {num(need.weight_kg)} кг ({w_note})",
        ]
        if need.manual:
            lines.append(f"Норма задана вручную: <b>{need.kcal:.0f} ккал/сутки</b>")
        else:
            lines.append(f"Стадия: {need.stage}, {num(need.factor)} × RER")
            lines.append(f"Суточная норма ≈ <b>{need.kcal:.0f} ккал</b>")
        for mode, insts in plans.items():
            items = []
            for inst in insts:
                for p in inst.task.portions:
                    if p.cat == cat.id:
                        g = portion_grams(cfg, p, need)
                        items.append(
                            f"  • {hm(inst.due)} {escape(inst.task.name)}: {p.share * 100:.0f}% → "
                            f"{format_grams(g)} {escape(cfg.food(p.food).name)}"
                        )
            if items:
                lines.append(f"<i>{escape(cfg.mode_titles.get(mode, mode))}:</i>")
                lines.extend(items)
        blocks.append("\n".join(lines))
    blocks.append(
        "ℹ️ Это стартовый расчёт. Сверяйтесь с таблицей на упаковке и взвешивайте кошек: "
        "котёнка — раз в неделю, взрослую — раз в 2–4 недели. Записать вес: /weight"
    )
    return "\n\n".join(blocks)


def weight_saved(cat_name: str, kg: float, history: list[tuple[date, float]], need: Need) -> str:
    lines = [f"⚖️ {escape(cat_name)}: {num(kg)} кг записано."]
    if len(history) >= 2:
        (d1, w1), (d0, w0) = history[0], history[1]
        days = max((d1 - d0).days, 1)
        delta = (w1 - w0) * 1000
        per_week = delta / days * 7
        lines.append(f"Изменение с {d0.strftime('%d.%m')}: {delta:+.0f} г ({per_week:+.0f} г в неделю).")
    lines.append(f"Новая суточная норма ≈ {need.kcal:.0f} ккал — порции в напоминаниях пересчитаны.")
    return "\n".join(lines)


def milestone(cat: Cat, key: str, need: Need) -> str:
    name = f"{cat.emoji} <b>{escape(cat.name)}</b>"
    kcal = f"Норма пересчитана: ≈ {need.kcal:.0f} ккал/сутки ({escape(need.stage)}). Порции в напоминаниях уже новые."
    if key == "4m":
        lines = [f"{name}: сегодня 4 месяца 🎉", kcal]
        if not cat.neutered:
            lines.append(
                "Пора записаться к ветеринару на стерилизацию — International Cat Care и «Feline Fix by Five» "
                "рекомендуют 4–5 месяцев (первая течка бывает уже с 4 месяцев). "
                "После операции впишите в config.toml <code>neutered = true</code> и <code>neutered_on = дата</code> — "
                "бот сам уменьшит норму."
            )
        return "\n".join(lines)
    if key == "6m":
        return "\n".join([
            f"{name}: сегодня 6 месяцев.",
            kcal,
            "С этого возраста достаточно 3 кормлений в день (минимум 2). Если хотите — уберите одно "
            "кормление из расписания и распределите его долю по остальным.",
        ])
    if key == "12m":
        return "\n".join([
            f"{name}: сегодня год! 🎂",
            kcal,
            "Пора переходить с корма для котят на корм для взрослых "
            + ("стерилизованных " if cat.neutered else "")
            + "кошек — постепенно, за 7–10 дней: 2 дня 25% нового, 2 дня 50%, 3 дня 75%, потом 100%. "
            "Не забудьте поменять корм и калорийность в config.toml.",
        ])
    if key == "spay":
        return "\n".join([
            f"{name}: день стерилизации.",
            "Первые две недели кормим по указаниям врача, норма не урезается. Потом бот сам уменьшит её на 10%: "
            "после стерилизации аппетит растёт, а расход энергии падает.",
            "Корм для котят оставляем до года. Взвешиваем каждую неделю: если вес растёт быстрее кривой роста "
            "или упитанность выше 5/9 — уберите ещё 5–10% (параметр kitten_spay_cut).",
        ])
    return f"{name}: {escape(key)}"


HELP = """🐾 <b>Кошачий бот</b> напоминает, когда кормить и поить кошек, и следит, чтобы никого не забыли и не покормили дважды.

<b>Кнопки внизу</b>
🏠 Я дома — пришёл(-ла) домой: бот сразу скажет, что нужно сделать
📋 Сегодня — что уже сделано и что ждёт
🚨 Что-то не так — кошка вялая, не ест, рвота… Родители узнают сразу

<b>Команды</b>
/today — план на сегодня: что сделано, что ждёт (и кнопки «Готово»)
/ration — сколько грамм давать каждой кошке и почему
/weight — записать вес: <code>/weight Младшая 1.6</code>
/plan — правила кормления коротко
/mode — режим дня: будни, выходной, каникулы (родители)
/pause и /resume — пауза, например на время отъезда (родители)
/help — эта справка

Когда приходит напоминание:
✅ Готово — нажми, как только поставил(а) миски (через полчаса бот спросит про остатки)
⏰ Через 10 мин — напомнить позже
🙅 Не могу — передать следующему, кто дома"""


def _name(cfg: Config, member_id: str | None) -> str:
    if not member_id:
        return "?"
    try:
        return cfg.member(member_id).name
    except KeyError:
        return member_id
