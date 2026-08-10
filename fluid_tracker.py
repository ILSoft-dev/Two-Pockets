"""
fluid_tracker.py
v1.0 - technical fluid replacement tracking (oil, antifreeze, etc.)

No engine-specific numbers hardcoded (the family has two different cars) —
generic, commonly-cited intervals per fluid TYPE, easy to adjust below if
they don't fit a specific car. "Which fluid was this repair about" is
detected by scanning the free-text Описание of Ремонт-type Авто rows for
keywords — no extra column/schema needed, reuses what's already stored.
"""
import aiohttp

import sheets_client as sc

FLUID_INTERVALS_KM = {
    "Масло": 10_000,
    "Антифриз": 60_000,
    "Тормозная жидкость": 40_000,
    "Фильтр воздуха": 15_000,
    "Свечи": 30_000,
}

FLUID_KEYWORDS = {
    # Одно слово — минимальная основа без окончания, чтобы ловить все
    # падежи ("масло"/"масла"/"маслом"/"масле" и т.д. — просто "масл").
    # Составной термин — кортеж из основ через AND (оба должны встретиться,
    # порядок и падеж не важны): "тормозную жидкость" и "тормозной жидкости"
    # ловятся одинаково через ("тормозн", "жидкост").
    "Масло": ["масл"],
    "Антифриз": ["антифриз", "охлажда"],
    "Тормозная жидкость": [("тормозн", "жидкост")],
    "Фильтр воздуха": [("фильтр", "возду")],
    "Свечи": ["свеч"],
}

# Предупреждаем, когда осталось меньше этой доли интервала (0.1 = последние 10%)
WARN_FRACTION = 0.1


def detect_fluid_type(text: str) -> str | None:
    lowered = text.lower()
    for fluid, keywords in FLUID_KEYWORDS.items():
        for kw in keywords:
            if isinstance(kw, tuple):
                if all(part in lowered for part in kw):
                    return fluid
            elif kw in lowered:
                return fluid
    return None


async def get_last_replacement_mileage(access_token: str, spreadsheet_id: str,
                                       car_name: str, fluid: str) -> float | None:
    """Deliberately does NOT require Тип=='Ремонт' — auto_expense's type
    heuristic and this module's fluid-keyword detection are independent
    classifiers that can disagree (e.g. "заправил антифриза" trips the
    Заправка keyword in auto_expense but is clearly an antifreeze
    replacement here). Matching on the fluid keyword alone is more robust
    than requiring both heuristics to agree."""
    async with aiohttp.ClientSession() as session:
        rows = await sc.get_rows(session, access_token, spreadsheet_id, sc.SHEET_AUTO)
    candidates = []
    for r in rows:
        if r["Машина"] != car_name:
            continue
        if detect_fluid_type(str(r["Описание"])) != fluid:
            continue
        if r["Пробег"]:
            candidates.append(sc.to_float(r["Пробег"]))
    return max(candidates) if candidates else None


async def check_due_fluids(access_token: str, spreadsheet_id: str, car_name: str,
                           current_mileage: float) -> list[dict]:
    """Fluids at/past the warning threshold. Fluids with NO logged
    replacement at all are silently skipped — we have no baseline to
    measure from, so flagging them would just be noise, not signal."""
    due = []
    for fluid, interval in FLUID_INTERVALS_KM.items():
        last_at = await get_last_replacement_mileage(access_token, spreadsheet_id, car_name, fluid)
        if last_at is None:
            continue
        due_at = last_at + interval
        remaining = due_at - current_mileage
        if remaining <= interval * WARN_FRACTION:
            due.append({
                "fluid": fluid, "last_at": last_at, "due_at": due_at, "remaining_km": remaining,
            })
    return due


def format_due_warning(due_list: list[dict], car_name: str) -> str:
    lines = [f"⚠️ «{car_name}»: пора проверить —"]
    for item in due_list:
        if item["remaining_km"] <= 0:
            lines.append(f"• {item['fluid']}: пробег уже {-item['remaining_km']:g} км сверх нормы")
        else:
            lines.append(f"• {item['fluid']}: осталось ~{item['remaining_km']:g} км")
    return "\n".join(lines)
