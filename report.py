import logging
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

import supabase_client as db
import sheets_transactions as tx
import narrative_report
from period_utils import period_start  # ре-экспорт: car_stats.py и др. импортируют period_start отсюда же

router = Router()


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
    except Exception:
        logging.exception("report: unexpected error fetching from Sheets")
        await message.answer(
            "Не получилось обратиться к Google Диску. Если повторится — "
            "переподключи через /start."
        )
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

    narrative = await narrative_report.build_narrative_for_user(
        user["id"], since, None, "текущий период", currency, "current_period",
    )
    if narrative:
        lines.append("")
        lines.append(narrative)

    await message.answer("\n".join(lines))
