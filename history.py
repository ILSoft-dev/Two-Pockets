"""
history.py
v2.0 - /history, now reading from Google Sheets (sheets_transactions) instead
of Supabase — see input_handler.py v2.0 changelog for why.
"""
import logging

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

import supabase_client as db
import sheets_transactions as tx

router = Router()

DEFAULT_DAYS = 7


@router.message(Command("history"))
async def cmd_history(message: Message, command: CommandObject):
    user = db.get_user(message.from_user.id)
    if not user or not user.get("onboarding_done"):
        await message.answer("Сначала пройди настройку: /start")
        return

    days = DEFAULT_DAYS
    if command.args and command.args.strip().isdigit():
        days = int(command.args.strip())

    try:
        rows = await tx.get_history(user["id"], days)
    except tx.NoGoogleAccount:
        await message.answer("Google Drive не подключён — пройди заново /start, чтобы подключить.")
        return
    except Exception:
        logging.exception("history: unexpected error fetching from Sheets")
        await message.answer(
            "Не получилось обратиться к Google Диску. Если повторится — "
            "переподключи через /start."
        )
        return
    currency = user.get("currency", "RUB")

    if not rows:
        await message.answer(f"За последние {days} дн. записей нет.")
        return

    lines = [f"🧾 <b>История за {days} дн.</b>\n"]
    last_day = None
    for r in rows:
        dt = str(r["Дата и время"])[:10]
        if dt != last_day:
            lines.append(f"\n<b>{dt}</b>")
            last_day = dt
        sign = "+" if r["Тип"] == "income" else "-"
        lines.append(f"{sign}{tx.to_float(r['Сумма']):g} {currency} — {r['Категория']}")

    await message.answer("\n".join(lines))
