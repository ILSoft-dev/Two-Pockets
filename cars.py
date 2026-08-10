"""
cars.py
v1.0 - car registry + mileage logging, backed by Google Sheets (sheets_client.py)

Cars live in the "Машины" tab (registry: name + status) and mileage points
in the "Пробег" tab. Both belong to whichever spreadsheet is the *effective*
one for a given user at call time (their own, or the family owner's) — this
module takes (access_token, spreadsheet_id) already resolved by the caller
via supabase_client.get_effective_google_account(), so it has no direct DB
dependency of its own.
"""
import re

import aiohttp

import sheets_client as sc

STATUS_ACTIVE = "Активна"
STATUS_ARCHIVED = "Архив"


async def add_car(access_token: str, spreadsheet_id: str, name: str,
                  who: str = "", starting_mileage: float | None = None) -> str:
    """Register a new car. If a starting mileage is given, also logs it as
    the first point in the mileage history (source: manual input) so
    average-km/month math has a real anchor from day one."""
    async with aiohttp.ClientSession() as session:
        car_id = await sc.append_row(
            session, access_token, spreadsheet_id, sc.SHEET_CARS,
            [name, STATUS_ACTIVE, sc.now_iso(), ""],
        )
        if starting_mileage is not None:
            await sc.append_row(
                session, access_token, spreadsheet_id, sc.SHEET_MILEAGE,
                [sc.now_iso(), name, starting_mileage, "Ручной ввод", who],
            )
    return car_id


async def list_active_cars(access_token: str, spreadsheet_id: str) -> list[dict]:
    async with aiohttp.ClientSession() as session:
        rows = await sc.get_rows(session, access_token, spreadsheet_id, sc.SHEET_CARS)
    return [r for r in rows if r["Статус"] == STATUS_ACTIVE]


def match_car_name(text: str, active_cars: list[dict]) -> str | None:
    """Substring match against registered car names (case-insensitive) —
    same cheap deterministic approach category_map already uses for
    keyword->category lookup, no LLM call needed for this."""
    lowered = text.lower()
    for car in active_cars:
        if car["Машина"].lower() in lowered:
            return car["Машина"]
    return None


async def get_latest_mileage(access_token: str, spreadsheet_id: str,
                             car_name: str) -> float | None:
    """Most recent mileage point for a car — used both for the reminder's
    'Без изменений' button (re-log the same value with today's date) and,
    later, for average-km/month math."""
    async with aiohttp.ClientSession() as session:
        rows = await sc.get_rows(session, access_token, spreadsheet_id, sc.SHEET_MILEAGE)
    points = [r for r in rows if r["Машина"] == car_name]
    if not points:
        return None
    points.sort(key=lambda r: str(r["Дата"]), reverse=True)
    return sc.to_float(points[0]["Пробег"])


async def archive_and_export_car(access_token: str, spreadsheet_id: str, car_id: str) -> str | None:
    """Removes a car from active tracking (soft — Статус -> Архив, no data
    deleted from the main spreadsheet) and exports its full history (auto
    expenses + mileage points) as a standalone spreadsheet with a summary
    tab. Returns the export's shareable link, or None if the car wasn't
    found (e.g. already removed)."""
    async with aiohttp.ClientSession() as session:
        cars_rows = await sc.get_rows(session, access_token, spreadsheet_id, sc.SHEET_CARS)
        car_row = next((c for c in cars_rows if c["ID"] == car_id), None)
        if not car_row:
            return None
        car_name = car_row["Машина"]

        auto_rows = [r for r in await sc.get_rows(session, access_token, spreadsheet_id, sc.SHEET_AUTO)
                    if r["Машина"] == car_name]
        mileage_rows = [r for r in await sc.get_rows(session, access_token, spreadsheet_id, sc.SHEET_MILEAGE)
                       if r["Машина"] == car_name]

        total_spent = sum(sc.to_float(r["Сумма"]) for r in auto_rows)
        by_type: dict[str, float] = {}
        for r in auto_rows:
            by_type[r["Тип"]] = by_type.get(r["Тип"], 0) + sc.to_float(r["Сумма"])
        mileages = [sc.to_float(r["Пробег"]) for r in mileage_rows if r["Пробег"]]

        summary_rows = [
            ["Показатель", "Значение"],
            ["Машина", car_name],
            ["Дата регистрации", car_row.get("Дата регистрации", "")],
            ["Всего потрачено", f"{total_spent:g}"],
        ]
        for t, amount in sorted(by_type.items()):
            summary_rows.append([f"  из них: {t}", f"{amount:g}"])
        if mileages:
            summary_rows.append(["Пробег: от", f"{min(mileages):g}"])
            summary_rows.append(["Пробег: до", f"{max(mileages):g}"])

        auto_headers = sc.HEADERS[sc.SHEET_AUTO]
        mileage_headers = sc.HEADERS[sc.SHEET_MILEAGE]
        auto_export = [auto_headers] + [[r[h] for h in auto_headers] for r in auto_rows]
        mileage_export = [mileage_headers] + [[r[h] for h in mileage_headers] for r in mileage_rows]

        export_id = await sc.create_plain_spreadsheet(
            session, access_token, f"Архив: {car_name}",
            {"Сводка": summary_rows, "Траты": auto_export, "Пробег": mileage_export},
        )
        link = await sc.publish_and_get_url(session, access_token, export_id)

        await sc.update_cell(
            session, access_token, spreadsheet_id, sc.SHEET_CARS, car_id, "Статус", STATUS_ARCHIVED
        )

    return link


def parse_mileage(text: str) -> float | None:
    """Pull a plain number out of free-form mileage input — accepts
    '305000', '305 000', '305.000', '305000 км', etc. Returns None if no
    digits at all (so the caller can ask again instead of saving garbage)."""
    digits = "".join(ch for ch in text if ch.isdigit())
    return float(digits) if digits else None


def parse_mileage_message(text: str) -> tuple[str, float | None]:
    """For standalone 'пробег опель 305000 км' messages (no currency, so
    parser.parse_amount would reject them). Strips the 'пробег' keyword,
    digits, and 'км' unit, leaving whatever's left for car-name matching.
    Returns (leftover_text, mileage)."""
    mileage = parse_mileage(text)
    working = re.sub(r"(?i)пробег[а-я]*", "", text)
    working = re.sub(r"[\d\s.,]+", " ", working)
    working = re.sub(r"(?i)\bкм\b", "", working)
    return working.strip(), mileage
