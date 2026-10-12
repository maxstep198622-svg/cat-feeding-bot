import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("CATBOT_JOIN_CODE", "test-code")

from catbot.config import parse_config  # noqa: E402


def base_raw():
    return {
        "timezone": "Europe/Moscow",
        "join_code": "cats-2026",
        "db_path": ":memory:",
        "members": [
            {"id": "papa", "name": "Папа", "role": "parent"},
            {"id": "tanya", "name": "Таня", "role": "parent"},
            {"id": "tasya", "name": "Тася", "role": "child"},
        ],
        "cats": [
            {"id": "kitten", "name": "Младшая", "birth_date": "2026-07-10", "weight_kg": 1.5, "emoji": "🐱"},
            {"id": "adult", "name": "Старшая", "birth_date": "2025-10-01", "weight_kg": 4.0, "neutered": True},
        ],
        "foods": [
            {"id": "kdry", "name": "сухой для котят", "kcal_per_100g": 400},
            {"id": "adry", "name": "сухой для стерилизованных", "kcal_per_100g": 360},
        ],
        "modes": {"holiday": "Каникулы"},
        "holidays": ["2026-11-04"],
        "periods": [{"start": "2026-12-29", "end": "2027-01-11", "mode": "holiday"}],
        "reminders": {"repeat_every_min": 10, "max_repeats": 2, "escalate_after_min": 30, "give_up_after_min": 150},
        "tasks": [
            {
                "id": "breakfast", "meal": "breakfast", "name": "Завтрак", "time": "07:45", "days": ["weekday"],
                "primary": "papa", "backup": "tanya", "water": "Поменять воду", "followup": True,
                "portions": [
                    {"cat": "kitten", "food": "kdry", "share": 0.3},
                    {"cat": "adult", "food": "adry", "share": 0.4},
                ],
            },
            {
                "id": "lunch", "meal": "lunch", "name": "Обед", "time": "14:30", "days": ["weekday"],
                "time_by_day": {"tue": "15:30"},
                "primary": "tasya", "backup": "tanya", "on_home": True, "followup": True,
                "steps": ["Закрой Младшую в комнате", "Положи корм"],
                "portions": [
                    {"cat": "kitten", "food": "kdry", "share": 0.3},
                    {"cat": "adult", "food": "adry", "share": 0.2},
                ],
            },
            {
                "id": "dinner", "meal": "dinner", "name": "Ужин", "time": "19:00", "days": ["all"],
                "primary": "tasya", "backup": "tanya",
                "portions": [
                    {"cat": "kitten", "food": "kdry", "share": 0.4},
                    {"cat": "adult", "food": "adry", "share": 0.4},
                ],
            },
            {
                "id": "wk_breakfast", "meal": "breakfast", "name": "Завтрак", "time": "08:30", "days": ["weekend", "holiday"],
                "primary": "tanya", "backup": "papa",
                "portions": [
                    {"cat": "kitten", "food": "kdry", "share": 0.3},
                    {"cat": "adult", "food": "adry", "share": 0.4},
                ],
            },
            {
                "id": "wk_lunch", "meal": "lunch", "name": "Обед", "time": "13:30", "days": ["weekend", "holiday"],
                "primary": "tasya", "backup": "papa",
                "portions": [
                    {"cat": "kitten", "food": "kdry", "share": 0.3},
                    {"cat": "adult", "food": "adry", "share": 0.2},
                ],
            },
            {"id": "weigh", "name": "Взвесить Младшую", "time": "11:00", "days": ["sun"], "primary": "tasya", "backup": "papa"},
        ],
    }


@pytest.fixture
def raw():
    return base_raw()


@pytest.fixture
def cfg(raw):
    return parse_config(raw)
