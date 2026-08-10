"""
reminders.py
v1.0 - weekly mileage check-in reminders

Triggered by an external cron ping (see main.py's /cron/mileage-reminders
route — same UptimeRobot pattern already used for /health), not by any
Telegram message from the user, since this needs to fire proactively on a
schedule regardless of whether anyone talks to the bot that day.

Each car tracks its OWN "Последнее напоминание" timestamp independently
(Машины sheet) — with cars typically registered at different times, this
naturally staggers reminders across multiple cars without any extra
scheduling logic; no need to hand-pick different times per car.
"""
from datetime import datetime, timedelta, timezone

import aiohttp

import supabase_client as db
import sheets_client as sc
import cars
from keyboards import mileage_reminder_keyboard

REMINDER_INTERVAL_DAYS = 7


def _parse_iso(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _due(car: dict) -> bool:
    last = _parse_iso(car.get("Последнее напоминание")) or _parse_iso(car.get("Дата регистрации"))
    if last is None:
        return True  # нет ни напоминаний, ни даты регистрации — лучше напомнить
    return datetime.now(timezone.utc) - last >= timedelta(days=REMINDER_INTERVAL_DAYS)


async def run_reminder_sweep(bot) -> int:
    """Called from the cron web route. Returns how many reminder messages
    were actually sent — handy as a quick sanity check in the response body
    (visible in the UptimeRobot dashboard/logs) without needing to dig
    through application logs separately."""
    sent = 0

    for account in db.list_google_connected_users():
        async with aiohttp.ClientSession() as session:
            active_cars = await cars.list_active_cars(
                account["google_access_token"], account["google_spreadsheet_id"]
            )
            due_cars = [c for c in active_cars if _due(c)]
            if not due_cars:
                continue

            recipients = db.get_spreadsheet_recipients(account["id"])
            for car in due_cars:
                for tg_id in recipients:
                    try:
                        await bot.send_message(
                            tg_id,
                            f"🚗 Какой сейчас пробег у «{car['Машина']}»?",
                            reply_markup=mileage_reminder_keyboard(car["ID"]),
                        )
                        sent += 1
                    except Exception:
                        # Пользователь мог заблокировать бота и т.п. — не
                        # роняем весь sweep из-за одного недоступного адресата.
                        pass

                await sc.update_cell(
                    session, account["google_access_token"], account["google_spreadsheet_id"],
                    sc.SHEET_CARS, car["ID"], "Последнее напоминание", sc.now_iso(),
                )

    return sent
