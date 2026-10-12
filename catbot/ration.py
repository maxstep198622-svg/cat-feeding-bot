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
    weight_kg: float
    age_months: float
    stage: str
    factor: float
    kcal: float
    manual: bool  # норма задана вручную (daily_kcal)


def stage_and_factor(cfg: Config, cat: Cat, today: date) -> tuple[str, float]:
    months = age_months(cat.birth_date, today)
    neutered = cat.neutered and (cat.neutered_on is None or cat.neutered_on <= today)
    e = cfg.energy
    if months < 12:
        if neutered:
            return "котёнок после стерилизации", e.kitten_neutered
        if months < 4:
            return "котёнок до 4 месяцев", e.kitten_under_4m
        return "котёнок 4–12 месяцев", e.kitten_4_to_12m
    if neutered:
        return "взрослая стерилизованная", e.adult_neutered
    return "взрослая нестерилизованная", e.adult_intact


def daily_need(cfg: Config, cat: Cat, today: date, weight_kg: float | None = None) -> Need:
    weight = weight_kg or cat.weight_kg
    stage, factor = stage_and_factor(cfg, cat, today)
    if cat.energy_factor:
        factor = cat.energy_factor
    if cat.daily_kcal:
        kcal, manual = cat.daily_kcal, True
    else:
        kcal, manual = rer(weight) * factor, False
    return Need(cat, weight, age_months(cat.birth_date, today), stage, factor, kcal, manual)


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
