"""Загрузка и проверка конфигурации бота (TOML)."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from datetime import date, time
from pathlib import Path
from zoneinfo import ZoneInfo

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
BUILTIN_MODES = ("weekday", "weekend")
TASK_KINDS = ("feeding", "water", "care")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Member:
    id: str
    name: str
    role: str  # parent | child
    telegram_id: int | None = None

    @property
    def is_parent(self) -> bool:
        return self.role == "parent"


@dataclass(frozen=True)
class Food:
    id: str
    name: str
    kcal_per_100g: float
    kind: str = "dry"  # dry | wet


@dataclass(frozen=True)
class Cat:
    id: str
    name: str
    birth_date: date
    neutered: bool
    weight_kg: float
    emoji: str = "🐈"
    neutered_on: date | None = None
    daily_kcal: float | None = None  # ручное значение от ветеринара — важнее расчёта
    energy_factor: float | None = None  # ручной множитель к RER


@dataclass(frozen=True)
class Portion:
    cat: str
    food: str
    share: float  # доля суточной калорийности кошки


@dataclass(frozen=True)
class Task:
    id: str
    name: str
    kind: str
    time: time
    days: tuple[str, ...]
    primary: str
    backup: str | None
    steps: tuple[str, ...] = ()
    water: str = ""
    portions: tuple[Portion, ...] = ()
    time_by_tag: dict[str, time] = field(default_factory=dict)
    escalate_after_min: int | None = None
    give_up_after_min: int | None = None


@dataclass(frozen=True)
class Period:
    start: date
    end: date
    mode: str


@dataclass(frozen=True)
class Reminders:
    repeat_every_min: int = 10
    max_repeats: int = 2
    snooze_min: int = 10
    escalate_after_min: int = 30
    give_up_after_min: int = 150
    daily_report: time | None = time(21, 45)
    notify_parents_on_child_done: bool = True


@dataclass(frozen=True)
class Energy:
    """Множители к RER = 70 × масса^0,75 по стадиям жизни."""

    kitten_under_4m: float = 3.0
    kitten_4_to_12m: float = 2.5
    kitten_neutered: float = 2.0
    adult_intact: float = 1.4
    adult_neutered: float = 1.2


@dataclass(frozen=True)
class Config:
    timezone: ZoneInfo
    join_code: str
    db_path: Path
    members: tuple[Member, ...]
    cats: tuple[Cat, ...]
    foods: tuple[Food, ...]
    tasks: tuple[Task, ...]
    modes: tuple[str, ...]
    mode_titles: dict[str, str]
    holidays: frozenset[date]
    periods: tuple[Period, ...]
    reminders: Reminders
    energy: Energy
    tick_seconds: int = 30

    def member(self, member_id: str) -> Member:
        for m in self.members:
            if m.id == member_id:
                return m
        raise KeyError(member_id)

    def cat(self, cat_id: str) -> Cat:
        for c in self.cats:
            if c.id == cat_id:
                return c
        raise KeyError(cat_id)

    def food(self, food_id: str) -> Food:
        for f in self.foods:
            if f.id == food_id:
                return f
        raise KeyError(food_id)

    def task(self, task_id: str) -> Task:
        for t in self.tasks:
            if t.id == task_id:
                return t
        raise KeyError(task_id)

    @property
    def parents(self) -> tuple[Member, ...]:
        return tuple(m for m in self.members if m.is_parent)


def _parse_time(value: str, where: str) -> time:
    try:
        hh, mm = value.split(":")
        return time(int(hh), int(mm))
    except (ValueError, AttributeError) as exc:
        raise ConfigError(f"{where}: время должно быть в формате ЧЧ:ММ, получено {value!r}") from exc


def _parse_date(value, where: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ConfigError(f"{where}: дата должна быть в формате ГГГГ-ММ-ДД, получено {value!r}") from exc


def _require(d: dict, key: str, where: str):
    if key not in d:
        raise ConfigError(f"{where}: не задано поле {key!r}")
    return d[key]


def _unique_ids(items, what: str) -> None:
    seen = set()
    for item in items:
        if item.id in seen:
            raise ConfigError(f"{what}: повторяется id {item.id!r}")
        seen.add(item.id)


def parse_config(raw: dict, base_dir: Path | None = None) -> Config:
    base_dir = base_dir or Path.cwd()

    tz_name = raw.get("timezone", "Europe/Moscow")
    try:
        tz = ZoneInfo(tz_name)
    except Exception as exc:  # ZoneInfoNotFoundError и ошибки формата
        raise ConfigError(f"timezone: неизвестный часовой пояс {tz_name!r}") from exc

    join_code = str(_require(raw, "join_code", "конфиг")).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{4,64}", join_code):
        raise ConfigError("join_code: 4–64 символа — латиница, цифры, _ или - (иначе не сработает ссылка-приглашение)")

    db_path = Path(raw.get("db_path", "data/catbot.sqlite3"))
    if not db_path.is_absolute():
        db_path = base_dir / db_path

    members = tuple(
        Member(
            id=str(_require(m, "id", "members")),
            name=str(_require(m, "name", "members")),
            role=str(m.get("role", "parent")),
            telegram_id=m.get("telegram_id"),
        )
        for m in raw.get("members", [])
    )
    if not members:
        raise ConfigError("members: нужен хотя бы один член семьи")
    _unique_ids(members, "members")
    for m in members:
        if m.role not in ("parent", "child"):
            raise ConfigError(f"members.{m.id}: role должен быть parent или child")
    if not any(m.is_parent for m in members):
        raise ConfigError("members: нужен хотя бы один родитель (role = \"parent\")")

    foods = tuple(
        Food(
            id=str(_require(f, "id", "foods")),
            name=str(_require(f, "name", "foods")),
            kcal_per_100g=float(_require(f, "kcal_per_100g", "foods")),
            kind=str(f.get("kind", "dry")),
        )
        for f in raw.get("foods", [])
    )
    _unique_ids(foods, "foods")
    for f in foods:
        if f.kcal_per_100g <= 0:
            raise ConfigError(f"foods.{f.id}: kcal_per_100g должен быть больше нуля")

    cats = []
    for c in raw.get("cats", []):
        where = f"cats.{c.get('id', '?')}"
        cats.append(
            Cat(
                id=str(_require(c, "id", "cats")),
                name=str(_require(c, "name", where)),
                birth_date=_parse_date(_require(c, "birth_date", where), where),
                neutered=bool(c.get("neutered", False)),
                weight_kg=float(_require(c, "weight_kg", where)),
                emoji=str(c.get("emoji", "🐈")),
                neutered_on=_parse_date(c["neutered_on"], where) if c.get("neutered_on") else None,
                daily_kcal=float(c["daily_kcal"]) if c.get("daily_kcal") else None,
                energy_factor=float(c["energy_factor"]) if c.get("energy_factor") else None,
            )
        )
    cats = tuple(cats)
    _unique_ids(cats, "cats")

    custom_modes = raw.get("modes", {})
    modes = BUILTIN_MODES + tuple(k for k in custom_modes if k not in BUILTIN_MODES)
    mode_titles = {"weekday": "Будни", "weekend": "Выходной"}
    mode_titles.update({k: str(v) for k, v in custom_modes.items()})
    valid_tags = set(WEEKDAYS) | set(modes) | {"all"}

    member_ids = {m.id for m in members}
    cat_ids = {c.id for c in cats}
    food_ids = {f.id for f in foods}

    tasks = []
    for t in raw.get("tasks", []):
        tid = str(_require(t, "id", "tasks"))
        where = f"tasks.{tid}"
        if "|" in tid or len(tid.encode()) > 30:
            raise ConfigError(f"{where}: id должен быть коротким и без символа |")
        days = t.get("days", ["all"])
        if isinstance(days, str):
            days = [days]
        for d in days:
            if d not in valid_tags:
                raise ConfigError(f"{where}: неизвестный день/режим {d!r}")
        primary = str(_require(t, "primary", where))
        backup = t.get("backup")
        for who in filter(None, (primary, backup)):
            if who not in member_ids:
                raise ConfigError(f"{where}: нет члена семьи {who!r}")
        portions = []
        for p in t.get("portions", []):
            if p.get("cat") not in cat_ids:
                raise ConfigError(f"{where}: нет кошки {p.get('cat')!r}")
            if p.get("food") not in food_ids:
                raise ConfigError(f"{where}: нет корма {p.get('food')!r}")
            share = float(_require(p, "share", where))
            if not 0 < share <= 1:
                raise ConfigError(f"{where}: share должен быть от 0 до 1")
            portions.append(Portion(cat=p["cat"], food=p["food"], share=share))
        time_by_tag = {}
        for tag, value in t.get("time_by_day", {}).items():
            if tag not in valid_tags:
                raise ConfigError(f"{where}.time_by_day: неизвестный день/режим {tag!r}")
            time_by_tag[tag] = _parse_time(value, f"{where}.time_by_day.{tag}")
        kind = str(t.get("kind", "feeding" if portions else "care"))
        if kind not in TASK_KINDS:
            raise ConfigError(f"{where}: kind должен быть одним из {TASK_KINDS}")
        tasks.append(
            Task(
                id=tid,
                name=str(_require(t, "name", where)),
                kind=kind,
                time=_parse_time(_require(t, "time", where), f"{where}.time"),
                days=tuple(days),
                primary=primary,
                backup=backup,
                steps=tuple(t.get("steps", [])),
                water=str(t.get("water", "")),
                portions=tuple(portions),
                time_by_tag=time_by_tag,
                escalate_after_min=t.get("escalate_after_min"),
                give_up_after_min=t.get("give_up_after_min"),
            )
        )
    tasks = tuple(tasks)
    _unique_ids(tasks, "tasks")

    holidays = frozenset(_parse_date(d, "holidays") for d in raw.get("holidays", []))
    periods = []
    for p in raw.get("periods", []):
        start = _parse_date(_require(p, "start", "periods"), "periods")
        end = _parse_date(_require(p, "end", "periods"), "periods")
        mode = str(_require(p, "mode", "periods"))
        if mode not in modes:
            raise ConfigError(f"periods: неизвестный режим {mode!r}")
        if end < start:
            raise ConfigError(f"periods: конец {end} раньше начала {start}")
        periods.append(Period(start, end, mode))

    rem_raw = dict(raw.get("reminders", {}))
    if "daily_report" in rem_raw:
        value = rem_raw["daily_report"]
        rem_raw["daily_report"] = _parse_time(value, "reminders.daily_report") if value else None
    try:
        reminders = Reminders(**rem_raw)
    except TypeError as exc:
        raise ConfigError(f"reminders: {exc}") from exc
    if reminders.escalate_after_min >= reminders.give_up_after_min:
        raise ConfigError("reminders: escalate_after_min должен быть меньше give_up_after_min")

    try:
        energy = Energy(**{k: float(v) for k, v in raw.get("energy", {}).items()})
    except TypeError as exc:
        raise ConfigError(f"energy: {exc}") from exc

    return Config(
        timezone=tz,
        join_code=join_code,
        db_path=db_path,
        members=members,
        cats=cats,
        foods=foods,
        tasks=tasks,
        modes=modes,
        mode_titles=mode_titles,
        holidays=holidays,
        periods=tuple(periods),
        reminders=reminders,
        energy=energy,
        tick_seconds=int(raw.get("tick_seconds", 30)),
    )


def load_config(path: str | Path) -> Config:
    path = Path(path)
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    return parse_config(raw, base_dir=path.resolve().parent)
