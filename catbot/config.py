"""Загрузка и проверка конфигурации бота (TOML)."""

from __future__ import annotations

import difflib
import re
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, time
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
    ideal_weight_kg: float | None = None  # для взрослой норму считают от идеального веса


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
    repeat_every_min: int | None = None
    max_repeats: int | None = None
    on_home: bool = False  # кнопка «Я дома» присылает задачу сразу, не дожидаясь времени
    followup: bool = False  # через followup_min спросить «остатки убраны? всё съела?»
    meal: str = ""  # ключ приёма: одинаковый у одного и того же кормления в разных режимах


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
    daily_report: time | None = time(22, 45)
    notify_parents_on_child_done: bool = True
    notify_parents_on_home: bool = True
    followup_min: int = 30
    home_window_min: int = 180  # «Я дома» подтягивает задачи, до которых осталось не больше этого


@dataclass(frozen=True)
class Energy:
    """Множители к RER = 70 × масса^0,75 по стадиям жизни (MSD, AAHA 2021, уравнение роста NRC 2006)."""

    kitten_0_4m: float = 2.5
    kitten_4_6m: float = 2.2
    kitten_6_9m: float = 1.9
    kitten_9_12m: float = 1.6
    kitten_spay_cut: float = 0.10  # на сколько уменьшить норму котёнка после стерилизации
    spay_recovery_days: int = 14  # первые дни после операции кормим по указаниям врача, без урезания
    adult_intact: float = 1.4
    adult_neutered: float = 1.1  # 1,2 — стройная и активная, 1,0 — полноватая или малоактивная


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
    if isinstance(value, datetime):
        return value.date()
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


def _int(value, where: str, minimum: int = 0, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where}: нужно целое число без кавычек, получено {value!r}")
    if value < minimum or (maximum is not None and value > maximum):
        upper = f"–{maximum}" if maximum is not None else " и больше"
        raise ConfigError(f"{where}: допустимо {minimum}{upper}, получено {value}")
    return value


def _num(value, where: str, lo: float, hi: float, hint: str = "") -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where}: нужно число без кавычек (дробная часть — через точку), получено {value!r}")
    if not lo <= value <= hi:
        raise ConfigError(f"{where}: {value:g} — вне разумных пределов {lo:g}–{hi:g}{hint}")
    return float(value)


def _bool(value, where: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{where}: нужно true или false (без кавычек), получено {value!r}")
    return value


def _str(value, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}: нужна непустая строка в кавычках, получено {value!r}")
    return value.strip()


def _table(value, where: str) -> dict:
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: ожидалась таблица")
    return value


def _tables(raw: dict, key: str) -> list[dict]:
    value = raw.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
        raise ConfigError(f"{key}: ожидался список таблиц [[{key}]]")
    return value


TOP_KEYS = {
    "timezone", "join_code", "db_path", "holidays", "tick_seconds", "reminders", "energy",
    "members", "cats", "foods", "modes", "periods", "tasks",
}
MEMBER_KEYS = {"id", "name", "role", "telegram_id"}
CAT_KEYS = {
    "id", "name", "emoji", "birth_date", "weight_kg", "neutered", "neutered_on",
    "daily_kcal", "energy_factor", "ideal_weight_kg",
}
FOOD_KEYS = {"id", "name", "kind", "kcal_per_100g", "kcal_per_kg"}
TASK_KEYS = {
    "id", "name", "kind", "time", "days", "time_by_day", "primary", "backup", "steps", "water", "portions",
    "escalate_after_min", "give_up_after_min", "repeat_every_min", "max_repeats", "on_home", "followup", "meal",
}
PORTION_KEYS = {"cat", "food", "share"}
PERIOD_KEYS = {"start", "end", "mode"}
REMINDER_INTS = {
    "repeat_every_min": 1, "max_repeats": 0, "snooze_min": 1, "escalate_after_min": 1,
    "give_up_after_min": 2, "followup_min": 1, "home_window_min": 0,
}
REMINDER_BOOLS = {"notify_parents_on_child_done", "notify_parents_on_home"}
REMINDER_KEYS = set(REMINDER_INTS) | REMINDER_BOOLS | {"daily_report"}
ENERGY_FACTORS = {"kitten_0_4m", "kitten_4_6m", "kitten_6_9m", "kitten_9_12m", "adult_intact", "adult_neutered"}
ENERGY_KEYS = ENERGY_FACTORS | {"kitten_spay_cut", "spay_recovery_days"}
FOOD_KCAL_RANGE = {"dry": (200, 650), "wet": (20, 300)}  # перекрываются: сырые и «свежие» корма бывают 200–250


def _check_keys(d: dict, allowed: set[str], where: str) -> None:
    for key in d:
        if key in allowed:
            continue
        msg = f"{where}: неизвестное поле {key!r}"
        close = difflib.get_close_matches(key, sorted(allowed), n=1)
        if close:
            msg += f" — может быть, {close[0]!r}?"
        if key in TOP_KEYS:
            msg += " Похоже, строка оказалась внутри таблицы: в TOML её нужно писать выше первого заголовка [...]."
        raise ConfigError(msg)


def _parse_member(m: dict) -> Member:
    where = f"members.{m.get('id', '?')}"
    _check_keys(m, MEMBER_KEYS, where)
    tg = m.get("telegram_id")
    if tg is not None:
        if isinstance(tg, str) and tg.strip().isdigit():
            tg = int(tg.strip())
        tg = _int(tg, f"{where}.telegram_id", 1)
    role = _str(m.get("role", "parent"), f"{where}.role")
    if role not in ("parent", "child"):
        raise ConfigError(f"{where}: role должен быть parent или child")
    return Member(
        id=_str(_require(m, "id", "members"), "members.id"),
        name=_str(_require(m, "name", where), f"{where}.name"),
        role=role,
        telegram_id=tg,
    )


def _parse_food(f: dict) -> Food:
    where = f"foods.{f.get('id', '?')}"
    _check_keys(f, FOOD_KEYS, where)
    kind = _str(f.get("kind", "dry"), f"{where}.kind")
    if kind not in FOOD_KCAL_RANGE:
        raise ConfigError(f"{where}: kind должен быть dry (сухой) или wet (влажный)")
    lo, hi = FOOD_KCAL_RANGE[kind]
    if "kcal_per_100g" in f:
        kcal100 = _num(f["kcal_per_100g"], f"{where}.kcal_per_100g", lo, hi,
                       " — проверьте упаковку; если там ккал/кг, пишите kcal_per_kg")
    elif "kcal_per_kg" in f:
        kcal100 = _num(f["kcal_per_kg"], f"{where}.kcal_per_kg", lo * 10, hi * 10,
                       " — проверьте упаковку; если там ккал/100 г, пишите kcal_per_100g") / 10
    else:
        raise ConfigError(f"{where}: укажите калорийность kcal_per_100g или kcal_per_kg (с упаковки)")
    return Food(
        id=_str(_require(f, "id", "foods"), "foods.id"),
        name=_str(_require(f, "name", where), f"{where}.name"),
        kcal_per_100g=kcal100,
        kind=kind,
    )


def _parse_cat(c: dict) -> Cat:
    where = f"cats.{c.get('id', '?')}"
    _check_keys(c, CAT_KEYS, where)

    def opt(key, lo, hi):
        return _num(c[key], f"{where}.{key}", lo, hi) if c.get(key) is not None else None

    return Cat(
        id=_str(_require(c, "id", "cats"), "cats.id"),
        name=_str(_require(c, "name", where), f"{where}.name"),
        birth_date=_parse_date(_require(c, "birth_date", where), where),
        neutered=_bool(c.get("neutered", False), f"{where}.neutered"),
        weight_kg=_num(_require(c, "weight_kg", where), f"{where}.weight_kg", 0.2, 15, " кг"),
        emoji=str(c.get("emoji", "🐈")),
        neutered_on=_parse_date(c["neutered_on"], where) if c.get("neutered_on") else None,
        daily_kcal=opt("daily_kcal", 30, 1000),
        energy_factor=opt("energy_factor", 0.5, 4),
        ideal_weight_kg=opt("ideal_weight_kg", 1, 15),
    )


def _parse_task(t: dict, valid_tags: set[str], member_ids: set[str], cat_ids: set[str], food_ids: set[str], rem: Reminders) -> Task:
    tid = _str(_require(t, "id", "tasks"), "tasks.id")
    where = f"tasks.{tid}"
    _check_keys(t, TASK_KEYS, where)
    if "|" in tid or ":" in tid or len(tid.encode()) > 30:
        raise ConfigError(f"{where}: id должен быть коротким (до 30 байт) и без символов | и :")
    days = t.get("days", ["all"])
    if isinstance(days, str):
        days = [days]
    if not isinstance(days, list) or not days:
        raise ConfigError(f"{where}.days: ожидался список, например [\"weekday\"]")
    for d in days:
        if d not in valid_tags:
            raise ConfigError(f"{where}: неизвестный день/режим {d!r}")
    primary = _str(_require(t, "primary", where), f"{where}.primary")
    backup = t.get("backup")
    if backup is not None:
        backup = _str(backup, f"{where}.backup")
    for who in filter(None, (primary, backup)):
        if who not in member_ids:
            raise ConfigError(f"{where}: нет члена семьи {who!r}")
    portions = []
    raw_portions = t.get("portions", [])
    if not isinstance(raw_portions, list):
        raise ConfigError(f"{where}.portions: ожидался список")
    for p in raw_portions:
        _check_keys(_table(p, f"{where}.portions"), PORTION_KEYS, f"{where}.portions")
        if p.get("cat") not in cat_ids:
            raise ConfigError(f"{where}: нет кошки {p.get('cat')!r}")
        if p.get("food") not in food_ids:
            raise ConfigError(f"{where}: нет корма {p.get('food')!r}")
        share = _num(_require(p, "share", where), f"{where}.share", 0, 1)
        if share == 0:
            raise ConfigError(f"{where}: share должен быть больше 0")
        portions.append(Portion(cat=p["cat"], food=p["food"], share=share))
    time_by_tag = {}
    for tag, value in _table(t.get("time_by_day", {}), f"{where}.time_by_day").items():
        if tag not in valid_tags:
            raise ConfigError(f"{where}.time_by_day: неизвестный день/режим {tag!r}")
        time_by_tag[tag] = _parse_time(value, f"{where}.time_by_day.{tag}")
    kind = _str(t.get("kind", "feeding" if portions else "care"), f"{where}.kind")
    if kind not in TASK_KINDS:
        raise ConfigError(f"{where}: kind должен быть одним из {TASK_KINDS}")
    steps = t.get("steps", [])
    if isinstance(steps, str):
        steps = [steps]
    if not isinstance(steps, list) or not all(isinstance(x, str) for x in steps):
        raise ConfigError(f"{where}.steps: ожидался список строк")

    def opt_int(key, minimum):
        return _int(t[key], f"{where}.{key}", minimum) if t.get(key) is not None else None

    escalate = opt_int("escalate_after_min", 1)
    give_up = opt_int("give_up_after_min", 2)
    if (escalate or rem.escalate_after_min) >= (give_up or rem.give_up_after_min):
        raise ConfigError(f"{where}: escalate_after_min должен быть меньше give_up_after_min (с учётом [reminders])")
    return Task(
        id=tid,
        name=_str(_require(t, "name", where), f"{where}.name"),
        kind=kind,
        time=_parse_time(_require(t, "time", where), f"{where}.time"),
        days=tuple(days),
        primary=primary,
        backup=backup,
        steps=tuple(steps),
        water=str(t.get("water", "")),
        portions=tuple(portions),
        time_by_tag=time_by_tag,
        escalate_after_min=escalate,
        give_up_after_min=give_up,
        repeat_every_min=opt_int("repeat_every_min", 1),
        max_repeats=opt_int("max_repeats", 0),
        on_home=_bool(t.get("on_home", False), f"{where}.on_home"),
        followup=_bool(t.get("followup", False), f"{where}.followup"),
        meal=_str(t.get("meal", tid), f"{where}.meal"),
    )


def _parse_reminders(raw: dict) -> Reminders:
    rem_raw = dict(_table(raw.get("reminders", {}), "reminders"))
    _check_keys(rem_raw, REMINDER_KEYS, "reminders")
    for key, minimum in REMINDER_INTS.items():
        if key in rem_raw:
            _int(rem_raw[key], f"reminders.{key}", minimum)
    for key in REMINDER_BOOLS:
        if key in rem_raw:
            _bool(rem_raw[key], f"reminders.{key}")
    if "daily_report" in rem_raw:
        value = rem_raw["daily_report"]
        rem_raw["daily_report"] = _parse_time(value, "reminders.daily_report") if value else None
    reminders = Reminders(**rem_raw)
    if reminders.escalate_after_min >= reminders.give_up_after_min:
        raise ConfigError("reminders: escalate_after_min должен быть меньше give_up_after_min")
    return reminders


def _parse_energy(raw: dict) -> Energy:
    e = _table(raw.get("energy", {}), "energy")
    _check_keys(e, ENERGY_KEYS, "energy")
    values = {}
    for key, value in e.items():
        if key in ENERGY_FACTORS:
            values[key] = _num(value, f"energy.{key}", 0.5, 4)
        elif key == "kitten_spay_cut":
            values[key] = _num(value, "energy.kitten_spay_cut", 0, 0.5, " (это доля: 0.1 = 10%)")
        else:
            values[key] = _int(value, f"energy.{key}", 0, 60)
    return Energy(**values)


def parse_config(raw: dict, base_dir: Path | None = None) -> Config:
    base_dir = base_dir or Path.cwd()
    _check_keys(raw, TOP_KEYS, "конфиг")

    tz_name = raw.get("timezone", "Europe/Moscow")
    try:
        tz = ZoneInfo(tz_name)
    except Exception as exc:  # ZoneInfoNotFoundError и ошибки формата
        raise ConfigError(f"timezone: неизвестный часовой пояс {tz_name!r}") from exc

    join_code = str(_require(raw, "join_code", "конфиг")).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{4,64}", join_code):
        raise ConfigError("join_code: 4–64 символа — латиница, цифры, _ или - (иначе не сработает ссылка-приглашение)")

    db_path = Path(_str(raw.get("db_path", "data/catbot.sqlite3"), "db_path"))
    if not db_path.is_absolute():
        db_path = base_dir / db_path

    members = tuple(_parse_member(m) for m in _tables(raw, "members"))
    if not members:
        raise ConfigError("members: нужен хотя бы один член семьи")
    _unique_ids(members, "members")
    if not any(m.is_parent for m in members):
        raise ConfigError("members: нужен хотя бы один родитель (role = \"parent\")")

    foods = tuple(_parse_food(f) for f in _tables(raw, "foods"))
    _unique_ids(foods, "foods")
    cats = tuple(_parse_cat(c) for c in _tables(raw, "cats"))
    _unique_ids(cats, "cats")

    custom_modes = _table(raw.get("modes", {}), "modes")
    for key, title in custom_modes.items():
        if not isinstance(title, str):
            raise ConfigError(
                f"modes.{key}: ожидалось название режима (строка). Если это не режим — перенесите строку "
                "выше заголовка [modes]: в TOML всё после [modes] попадает в эту таблицу."
            )
        if not re.fullmatch(r"[a-z0-9_]{1,20}", key) or key in WEEKDAYS or key == "all":
            raise ConfigError(f"modes.{key}: имя режима — латиница в нижнем регистре, цифры и _")
    modes = BUILTIN_MODES + tuple(k for k in custom_modes if k not in BUILTIN_MODES)
    mode_titles = {"weekday": "Будни", "weekend": "Выходной"}
    mode_titles.update({k: str(v) for k, v in custom_modes.items()})
    valid_tags = set(WEEKDAYS) | set(modes) | {"all"}

    reminders = _parse_reminders(raw)
    member_ids = {m.id for m in members}
    tasks = tuple(
        _parse_task(t, valid_tags, member_ids, {c.id for c in cats}, {f.id for f in foods}, reminders)
        for t in _tables(raw, "tasks")
    )
    _unique_ids(tasks, "tasks")

    holidays_raw = raw.get("holidays", [])
    if not isinstance(holidays_raw, list):
        raise ConfigError("holidays: ожидался список дат, например [\"2026-11-04\"]")
    holidays = frozenset(_parse_date(d, "holidays") for d in holidays_raw)
    periods = []
    for p in _tables(raw, "periods"):
        _check_keys(p, PERIOD_KEYS, "periods")
        start = _parse_date(_require(p, "start", "periods"), "periods")
        end = _parse_date(_require(p, "end", "periods"), "periods")
        mode = str(_require(p, "mode", "periods"))
        if mode not in modes:
            raise ConfigError(f"periods: неизвестный режим {mode!r}")
        if end < start:
            raise ConfigError(f"periods: конец {end} раньше начала {start}")
        periods.append(Period(start, end, mode))

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
        energy=_parse_energy(raw),
        tick_seconds=_int(raw.get("tick_seconds", 30), "tick_seconds", 5, 300),
    )


def load_config(path: str | Path) -> Config:
    path = Path(path)
    with path.open("rb") as fh:
        try:
            raw = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(
                f"{path.name}: ошибка в записи TOML ({exc}). Частые причины: дробные числа — через точку (1.5), "
                "текст — в кавычках, один ключ нельзя задать дважды."
            ) from exc
    return parse_config(raw, base_dir=path.resolve().parent)
