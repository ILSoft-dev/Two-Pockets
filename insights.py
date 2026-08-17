"""
insights.py
v1.0 - чат-вопросы по уже накопленным данным ("сколько потратил на корм в
июне?", "какой пробег у матиза за август?").

Архитектура: LLM (groq_client.parse_question) только РАЗБИРАЕТ вопрос в
структурированный запрос — категорию/машину/период. Сам ответ считает наш
код по данным из Sheets, без всякой фантазии модели в цифрах.

Разрешение года для месяца без явного года — правило "самый недавний
прошедший такой месяц": если месяц уже был в этом году — берём этот год,
если ещё не наступил — прошлый год. Явно названный год всегда побеждает.
"""
from datetime import datetime, timezone
from calendar import monthrange
import logging

import supabase_client as db
import cars
import groq_client
from report import period_start
from sheets_transactions import get_transactions_in_range, to_float, NoGoogleAccount

MONTH_NAMES = {
    1: "январь", 2: "февраль", 3: "март", 4: "апрель", 5: "май", 6: "июнь",
    7: "июль", 8: "август", 9: "сентябрь", 10: "октябрь", 11: "ноябрь", 12: "декабрь",
}


def resolve_year_for_month(month: int, explicit_year: int | None) -> int:
    """"В июне?" без года — самый недавний ПРОШЕДШИЙ июнь: этот год, если
    месяц уже наступил, иначе прошлый (этот июнь ещё не начался — значит,
    данных там быть не может, спрашивают про прошлый)."""
    if explicit_year is not None:
        return explicit_year
    now = datetime.now(timezone.utc)
    return now.year if month <= now.month else now.year - 1


def month_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    since = datetime(year, month, 1, tzinfo=timezone.utc)
    last_day = monthrange(year, month)[1]
    until = datetime(year, month, last_day, 23, 59, 59, tzinfo=timezone.utc)
    return since, until


def resolve_period(period_type: str, month: int | None, year: int | None,
                   month_start_day: int) -> tuple[datetime | None, datetime | None, str]:
    """Возвращает (since, until, human_label). until=None — открытый диапазон
    (до сейчас)."""
    if period_type == "specific_month" and month:
        actual_year = resolve_year_for_month(month, year)
        since, until = month_bounds(actual_year, month)
        label = f"{MONTH_NAMES.get(month, month)} {actual_year}"
        return since, until, label

    if period_type == "current_period":
        since = period_start(month_start_day)
        return since, None, "текущий период"

    # all_time или что-то неожиданное — без ограничений
    return None, None, "всё время"


async def answer_question(user_id: int, text: str) -> str:
    """Никогда не поднимает исключение наружу — любой сбой (сеть,
    Supabase, Groq) превращается в вежливое сообщение, а не в тишину."""
    try:
        return await _answer_question_inner(user_id, text)
    except Exception:
        logging.exception("answer_question: unexpected error")
        return "Не получилось ответить на вопрос — попробуй ещё раз позже."


async def _answer_question_inner(user_id: int, text: str) -> str:
    user = db.get_user_by_id(user_id)
    if not user:
        return "Не нашёл твой профиль — попробуй /start."

    account = db.get_effective_google_account(user_id)
    active_cars = []
    if account:
        try:
            active_cars = await cars.list_active_cars(account)
        except Exception:
            active_cars = []  # не критично для вопросов не про машины

    categories = [c["name"] for c in db.get_categories(user_id)]
    car_names = [c["Машина"] for c in active_cars]

    parsed = groq_client.parse_question(text, categories, car_names)
    if parsed is None:
        return (
            "Не понял вопрос 🤔 Попробуй переформулировать, например: "
            "«сколько я потратил на продукты в июле?» или "
            "«какой пробег у опеля за август?»."
        )

    intent = parsed.get("intent", "unknown")
    if intent == "unknown":
        return (
            "Не понял, о чём вопрос — про траты, доходы или пробег? "
            "Попробуй переформулировать."
        )

    since, until, label = resolve_period(
        parsed.get("period_type", "current_period"),
        parsed.get("month"), parsed.get("year"),
        user.get("month_start", 1),
    )

    if intent == "mileage":
        return await _answer_mileage(account, parsed.get("car_name"), since, until, label)

    return await _answer_money(user_id, intent, parsed.get("category"), parsed.get("item"),
                               since, until, label, user.get("currency", "RUB"))


def _item_matches(item: str, text: str) -> bool:
    """Подстрочное совпадение с запасом на падежные окончания ("мороженое"
    в вопросе должно найти "мороженых" в описании траты) — берём основу
    слова (~70% длины, минимум 3 символа), а не всё слово целиком. Та же
    идея, что уже применяли для распознавания жидкостей в fluid_tracker.py."""
    item_lower = item.lower().strip()
    text_lower = text.lower()
    if item_lower in text_lower:
        return True
    stem_len = max(3, int(len(item_lower) * 0.7))
    return item_lower[:stem_len] in text_lower


async def _answer_money(user_id: int, intent: str, category: str | None, item: str | None,
                        since, until, label: str, currency: str) -> str:
    try:
        rows = await get_transactions_in_range(user_id, since, until)
    except NoGoogleAccount:
        return "Google Drive не подключён — пройди заново /start."
    except Exception:
        return "Не получилось обратиться к Google Диску. Попробуй ещё раз позже."

    tx_type = "income" if intent == "income" else "expense"
    filtered = [r for r in rows if r["Тип"] == tx_type]

    # "item" (конкретный товар) приоритетнее "category" — если товар назван,
    # ищем именно его в описании траты, а не суммируем всю угаданную
    # категорию целиком (иначе "сколько на мороженое" отвечало бы суммой
    # по всей категории "Продукты", как это было до фикса).
    if item:
        filtered = [r for r in filtered if _item_matches(item, str(r.get("Комментарий", "")))]
        subject = f" на «{item}»"
    elif category:
        filtered = [r for r in filtered if r["Категория"] == category]
        subject = f" на «{category}»"
    else:
        subject = ""

    total = sum(to_float(r["Сумма"]) for r in filtered)
    verb = "доход" if intent == "income" else "расход"
    cat_part = subject

    if not filtered:
        return f"За {label}{cat_part} {verb}ов не нашёл."
    return f"За {label}{cat_part}: {verb} {total:g} {currency} ({len(filtered)} записей)."


async def _answer_mileage(account: dict | None, car_name: str | None,
                          since, until, label: str) -> str:
    if not account:
        return "Google Drive не подключён — пройди заново /start."
    if not car_name:
        return "Не понял, про какую машину вопрос — назови её явно."

    try:
        distance = await cars.get_mileage_distance(account, car_name, since, until)
    except Exception:
        return "Не получилось обратиться к Google Диску. Попробуй ещё раз позже."

    if distance is None:
        return f"За {label} нет данных о пробеге «{car_name}»."
    return f"За {label} «{car_name}» проехала {distance:g} км."
