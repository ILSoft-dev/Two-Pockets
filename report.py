from datetime import datetime, timezone
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

import supabase_client as db
import sheets_transactions as tx

router = Router()


def period_start(month_start_day: int) -> datetime:
    now = datetime.now(timezone.utc)
    if now.day >= month_start_day:
        start = now.replace(day=month_start_day, hour=0, minute=0, second=0, microsecond=0)
    else:
        # период начался в предыдущем месяце
        prev_month = now.month - 1 or 12
        prev_year = now.year if now.month > 1 else now.year - 1
        start = now.replace(
            year=prev_year, month=prev_month, day=month_start_day,
            hour=0, minute=0, second=0, microsecond=0,
        )
    return start


@router.message(Command("report"))
async def cmd_report(message: Message):
    user = db.get_user(message.from_user.id)
    if not user or not user.get("onboarding_done"):
        await message.answer("Сначала пройди настройку: /start")
        return

    since = period_start(user.get("month_start", 1))
    try:
        data = await tx.get_report(user["id"], since)
    except tx.NoGoogleAccount:
        await message.answer("Google Drive не подключён — пройди заново /start, чтобы подключить.")
        return
    currency = user.get("currency", "RUB")

    lines = [
        f"📊 <b>Отчёт с {since.strftime('%d.%m')}</b>\n",
        f"💰 Доход: {data['income']:g} {currency}",
        f"💸 Расход: {data['expense']:g} {currency}",
        f"Остаток: {data['balance']:g} {currency}\n",
    ]

    if data["top5"]:
        lines.append("Топ категорий расходов:")
        for i, (category, amount) in enumerate(data["top5"], start=1):
            lines.append(f"{i}. {category}: {amount:g} {currency}")
    else:
        lines.append("Расходов за период пока нет.")

    await message.answer("\n".join(lines))
