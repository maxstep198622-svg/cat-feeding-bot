"""Расчёт суточной нормы и порций.

RER (энергия покоя) = 70 × масса^0,75 ккал; суточная норма = RER × множитель стадии жизни.
Это стартовый ориентир: корректируется по взвешиванию и оценке упитанности (BCS) раз в 2–4 недели.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .config import Cat, Config, Food, Portion


def rer(weight_kg: float) -> float:
    return 70 * weight_kg**0.75


def age_months(birth: date, today: date) -> float:
    return (today - birth).days / 30.44


@dataclass(frozen=True)
class Need:
    cat: Cat
    weight_kg: float  # вес, от которого считали норму
    age_months: float
    stage: str
    factor: float
    kcal: float
    manual: bool  # норма задана вручную (daily_kcal)
    note: str = ""  # предупреждение для родителей, если расчёт выглядит странно


def _spayed_on(cat: Cat, today: date) -> bool:
    return cat.neutered and (cat.neutered_on is None or cat.neutered_on <= today)


def stage_and_factor(cfg: Config, cat: Cat, today: date) -> tuple[str, float]:
    months = age_months(cat.birth_date, today)
    e = cfg.energy
    if months >= 12:
        if _spayed_on(cat, today):
            return "взрослая стерилизованная", e.adult_neutered
        return "взрослая нестерилизованная", e.adult_intact
    if months < 4:
        stage, factor = "котёнок до 4 месяцев", e.kitten_0_4m
    elif months < 6:
        stage, factor = "котёнок 4–6 месяцев", e.kitten_4_6m
    elif months < 9:
        stage, factor = "котёнок 6–9 месяцев", e.kitten_6_9m
    else:
        stage, factor = "котёнок 9–12 месяцев", e.kitten_9_12m
    if _spayed_on(cat, today):
        days_after = (today - cat.neutered_on).days if cat.neutered_on else e.spay_recovery_days
        if days_after >= e.spay_recovery_days:
            factor *= 1 - e.kitten_spay_cut
            stage += f", стерилизована (−{e.kitten_spay_cut * 100:.0f}%)"
        else:
            stage += ", восстановление после стерилизации — кормим по указаниям врача"
    return stage, factor


def daily_need(cfg: Config, cat: Cat, today: date, weight_kg: float | None = None) -> Need:
    """Норма котёнка — от текущего веса, взрослой — от идеального (если он задан)."""
    weight = weight_kg or cat.weight_kg
    stage, factor = stage_and_factor(cfg, cat, today)
    months = age_months(cat.birth_date, today)
    if months >= 12 and cat.ideal_weight_kg:
        weight = cat.ideal_weight_kg
    if cat.energy_factor:
        factor = cat.energy_factor
    if cat.daily_kcal:
        kcal, manual = cat.daily_kcal, True
    else:
        kcal, manual = rer(weight) * factor, False
    note = ""
    per_kg = kcal / weight
    if months < 4 and not _spayed_on(cat, today) and not 150 <= per_kg <= 220:
        note = f"{per_kg:.0f} ккал на кг — вне обычного для 3–4 мес. коридора 150–220. Проверьте вес и обсудите с ветеринаром."
    return Need(cat, weight, months, stage, factor, kcal, manual, note)


def grams(kcal: float, food: Food) -> float:
    return kcal / food.kcal_per_100g * 100


def portion_grams(cfg: Config, portion: Portion, need: Need) -> float:
    return grams(need.kcal * portion.share, cfg.food(portion.food))


def format_grams(value: float) -> str:
    if value < 10:
        return f"{value:.1f}".replace(".", ",") + " г"
    return f"{round(value)} г"


def add_months(day: date, months: int) -> date:
    month = day.month - 1 + months
    year = day.year + month // 12
    month = month % 12 + 1
    for d in (day.day, 30, 29, 28):
        try:
            return date(year, month, d)
        except ValueError:
            continue
    raise ValueError(day)


MILESTONE_MONTHS = (4, 6, 12)
MILESTONE_WINDOW_DAYS = 7


def due_milestones(cat: Cat, today: date) -> list[tuple[str, date]]:
    """Возрастные вехи и день стерилизации, о которых пора сообщить родителям (окно — неделя)."""
    result = []
    for months in MILESTONE_MONTHS:
        when = add_months(cat.birth_date, months)
        if 0 <= (today - when).days < MILESTONE_WINDOW_DAYS:
            result.append((f"{months}m", when))
    if cat.neutered and cat.neutered_on and 0 <= (today - cat.neutered_on).days < MILESTONE_WINDOW_DAYS:
        if age_months(cat.birth_date, cat.neutered_on) < 12:
            result.append(("spay", cat.neutered_on))
    return result
