"""
insights.py
v1.1 - чат-вопросы по уже накопленным данным ("сколько потратил на корм в
июне?", "какой пробег у матиза за август?", "когда я покупал подписку на
Claude?"), с разговорной памятью для уточняющих вопросов ("а в июле?").

Архитектура: LLM (groq_client.parse_question) только РАЗБИРАЕТ вопрос в
структурированный запрос — категорию/машину/период. Сам ответ считает наш
код по данным из Sheets, без всякой фантазии модели в цифрах.

Разрешение года для месяца без явного года — правило "самый недавний
прошедший такой месяц": если месяц уже был в этом году — берём этот год,
если ещё не наступил — прошлый год. Явно названный год всегда побеждает.

Разговорная память: последний УСПЕШНО разобранный вопрос (intent != unknown)
сохраняется в Redis на 10 минут, keyed по user_id (не по эффективному
аккаунту — у каждого в семье своя, независимая "память" разговора). Новый
вопрос передаётся в LLM вместе с этим контекстом — сама модель решает,
уточнение это ("а в июле?") или самостоятельный новый вопрос, и что из
предыдущего разбора унаследовать.
"""
from datetime import datetime, timezone
import json
import logging

import redis.asyncio as redis_asyncio

import supabase_client as db
import cars
import groq_client
from config import REDIS_URL
from period_utils import (
    MONTH_NAMES,
    resolve_period,
    previous_period_bounds,
    format_date_human,
)
from sheets_transactions import get_transactions_in_range, to_float, NoGoogleAccount

_redis = redis_asyncio.from_url(REDIS_URL)
CONTEXT_TTL_SECONDS = 600  # 10 минут — то же окно, что у OAuth-state в google_oauth.py
_CONTEXT_KEY_PREFIX = "qa_context:"


async def _get_previous_context(user_id: int) -> dict | None:
    raw = await _redis.get(f"{_CONTEXT_KEY_PREFIX}{user_id}")
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


async def _save_context(user_id: int, parsed: dict) -> None:
    await _redis.set(
        f"{_CONTEXT_KEY_PREFIX}{user_id}", json.dumps(parsed, ensure_ascii=False),
        ex=CONTEXT_TTL_SECONDS,
    )


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

    try:
        previous_context = await _get_previous_context(user_id)
    except Exception:
        previous_context = None  # сбой Redis не должен ломать сам вопрос

    parsed = groq_client.parse_question(text, categories, car_names, previous_context)
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

    # Сохраняем контекст только при успешном разборе — чтобы неудачный
    # парсинг не "заразил" следующий вопрос мусорным контекстом.
    try:
        await _save_context(user_id, parsed)
    except Exception:
        pass  # не критично — просто не будет работать уточнение в этот раз

    answer_type = parsed.get("answer_type", "sum")
    period_type = parsed.get("period_type", "current_period")

    # Для "когда" — если период не назван явно конкретным месяцем, ищем за
    # всё время, а не только текущий период. Не полагаемся только на то,
    # что LLM сама применит это правило из промпта — дублируем в коде для
    # надёжности (см. урок с response_format в groq_client.py — доверять
    # модели там, где можно проверить самим, не стоит).
    if answer_type == "when" and period_type != "specific_month":
        period_type = "all_time"

    since, until, label = resolve_period(
        period_type, parsed.get("month"), parsed.get("year"),
        user.get("month_start", 1),
    )

    if intent == "mileage":
        return await _answer_mileage(account, parsed.get("car_name"), since, until, label)

    if intent == "compare_periods":
        return await _answer_compare_periods(
            user_id, parsed.get("category"), parsed.get("item"),
            since, until, label, user.get("currency", "RUB"), period_type,
        )

    if intent == "top_category":
        return await _answer_top_category(user_id, since, until, label, user.get("currency", "RUB"))

    # spending/income — если названа конкретная машина, ищем в листе "Авто"
    # (там есть колонка "Машина", в общем списке трат её нет вообще).
    car_name = parsed.get("car_name")
    if car_name and account:
        return await _answer_car_money(account, car_name, parsed.get("item"),
                                       since, until, label, user.get("currency", "RUB"), answer_type)

    return await _answer_money(user_id, intent, parsed.get("category"), parsed.get("item"),
                               since, until, label, user.get("currency", "RUB"), answer_type)


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


def _format_when_answer(filtered: list[dict], subject: str | None, label: str, currency: str,
                        date_field: str = "Дата и время") -> str:
    subject_part = f" «{subject}»" if subject else ""

    if not filtered:
        scope = "" if label == "всё время" else f" за {label}"
        return f"Не нашёл записей{subject_part}{scope}."

    def _dt(r):
        try:
            return datetime.fromisoformat(str(r[date_field]))
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)

    sorted_rows = sorted(filtered, key=_dt, reverse=True)
    latest = sorted_rows[0]
    date_str = format_date_human(latest[date_field])
    amount_str = f"{to_float(latest['Сумма']):g} {currency}"

    if len(sorted_rows) == 1:
        return f"{date_str}{subject_part}: {amount_str}."
    return (
        f"Последний раз{subject_part} — {date_str} ({amount_str}). "
        f"Всего таких записей: {len(sorted_rows)}."
    )


def _format_average(filtered: list[dict], subject: str | None, label: str, currency: str,
                    date_field: str = "Дата и время") -> str:
    """"В среднем трачу на X" — группируем по календарному месяцу (не по
    пользовательскому дню начала периода — так предсказуемее и не зависит
    от произвольной настройки), считаем среднее по месяцам, где хоть
    что-то было."""
    subject_part = f" на «{subject}»" if subject else ""
    if not filtered:
        return f"За {label}{subject_part} записей не нашёл — не могу посчитать среднее."

    by_month: dict[str, float] = {}
    for r in filtered:
        try:
            dt = datetime.fromisoformat(str(r[date_field]))
        except ValueError:
            continue
        key = f"{dt.year}-{dt.month:02d}"
        by_month[key] = by_month.get(key, 0) + to_float(r["Сумма"])

    if not by_month:
        return f"Не получилось посчитать среднее{subject_part}."

    avg = sum(by_month.values()) / len(by_month)
    return (
        f"В среднем{subject_part}: {avg:g} {currency} в месяц "
        f"(по данным за {len(by_month)} мес., {label})."
    )


async def _answer_money(user_id: int, intent: str, category: str | None, item: str | None,
                        since, until, label: str, currency: str, answer_type: str = "sum") -> str:
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
    subject = None
    if item:
        filtered = [r for r in filtered if _item_matches(item, str(r.get("Комментарий", "")))]
        subject = item
    elif category:
        filtered = [r for r in filtered if r["Категория"] == category]
        subject = category

    if answer_type == "when":
        return _format_when_answer(filtered, subject, label, currency)

    if answer_type == "count":
        cat_part = f" на «{subject}»" if subject else ""
        if not filtered:
            return f"За {label}{cat_part} записей не нашёл."
        return f"За {label}{cat_part}: {len(filtered)} раз(а)."

    if answer_type == "average":
        return _format_average(filtered, subject, label, currency)

    total = sum(to_float(r["Сумма"]) for r in filtered)
    verb = "доход" if intent == "income" else "расход"
    cat_part = f" на «{subject}»" if subject else ""

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


async def _answer_car_money(account: dict, car_name: str, item: str | None,
                            since, until, label: str, currency: str, answer_type: str) -> str:
    """Деньги + конкретная машина одновременно ("сколько на бензин для
    опеля") — ищем в листе 'Авто', а не в общем списке трат: там есть
    колонка 'Машина', в общем списке её нет вообще."""
    try:
        rows = await cars.get_auto_expenses_in_range(account, car_name, since, until)
    except Exception:
        return "Не получилось обратиться к Google Диску. Попробуй ещё раз позже."

    filtered = rows
    if item:
        filtered = [r for r in filtered if _item_matches(item, str(r.get("Описание", "")))]
        subject = f"«{item}» для «{car_name}»"
    else:
        subject = f"«{car_name}»"

    if answer_type == "when":
        return _format_when_answer(filtered, subject, label, currency, date_field="Дата")
    if answer_type == "count":
        if not filtered:
            return f"За {label} на {subject} записей не нашёл."
        return f"За {label} на {subject}: {len(filtered)} раз(а)."
    if answer_type == "average":
        return _format_average(filtered, subject, label, currency, date_field="Дата")

    total = sum(to_float(r["Сумма"]) for r in filtered)
    if not filtered:
        return f"За {label} на {subject} трат не нашёл."
    return f"За {label} на {subject}: {total:g} {currency} ({len(filtered)} записей)."


async def _answer_compare_periods(user_id: int, category: str | None, item: str | None,
                                  since: datetime, until, label: str, currency: str,
                                  period_type: str) -> str:
    """"В этом месяце потратил больше, чем в прошлом?" — сравниваем ОДИН
    названный период (since/until/label, уже разрешённые как обычно) с
    периодом сразу перед ним той же длины (или предыдущим календарным
    месяцем, если период — конкретный месяц)."""
    prev_since, prev_until = previous_period_bounds(since, until, period_type)
    if period_type == "specific_month":
        prev_label = f"{MONTH_NAMES.get(prev_since.month, prev_since.month)} {prev_since.year}"
    else:
        prev_label = "предыдущий период"

    try:
        current_rows = await get_transactions_in_range(user_id, since, until)
        prev_rows = await get_transactions_in_range(user_id, prev_since, prev_until)
    except NoGoogleAccount:
        return "Google Drive не подключён — пройди заново /start."
    except Exception:
        return "Не получилось обратиться к Google Диску. Попробуй ещё раз позже."

    def _sum_expenses(rows: list[dict]) -> float:
        filtered = [r for r in rows if r["Тип"] == "expense"]
        if item:
            filtered = [r for r in filtered if _item_matches(item, str(r.get("Комментарий", "")))]
        elif category:
            filtered = [r for r in filtered if r["Категория"] == category]
        return sum(to_float(r["Сумма"]) for r in filtered)

    current_total = _sum_expenses(current_rows)
    prev_total = _sum_expenses(prev_rows)
    subject = f" на «{item or category}»" if (item or category) else ""

    if prev_total == 0:
        if current_total == 0:
            return f"И за {label}, и за {prev_label}{subject} трат не было."
        return (
            f"За {label}{subject}: {current_total:g} {currency}. За {prev_label} "
            "трат не было — не с чем сравнить в процентах."
        )

    delta = current_total - prev_total
    if delta == 0:
        return (
            f"За {label}{subject}: {current_total:g} {currency} — ровно "
            f"столько же, сколько за {prev_label}."
        )

    pct = abs(delta / prev_total * 100)
    direction = "больше" if delta > 0 else "меньше"
    return (
        f"За {label}{subject}: {current_total:g} {currency} — это {direction} "
        f"на {pct:.0f}% ({abs(delta):g} {currency}), чем за {prev_label} "
        f"({prev_total:g} {currency})."
    )


async def _answer_top_category(user_id: int, since, until, label: str, currency: str,
                               top_n: int = 3) -> str:
    """"Какая моя самая большая категория трат за период?" — группируем по
    категории, сортируем по сумме, показываем топ-N (по умолчанию 3)."""
    try:
        rows = await get_transactions_in_range(user_id, since, until)
    except NoGoogleAccount:
        return "Google Drive не подключён — пройди заново /start."
    except Exception:
        return "Не получилось обратиться к Google Диску. Попробуй ещё раз позже."

    expenses = [r for r in rows if r["Тип"] == "expense"]
    if not expenses:
        return f"За {label} трат не было."

    by_category: dict[str, float] = {}
    for r in expenses:
        cat = r["Категория"]
        by_category[cat] = by_category.get(cat, 0) + to_float(r["Сумма"])

    ranked = sorted(by_category.items(), key=lambda kv: kv[1], reverse=True)
    lines = [f"Топ категорий трат за {label}:"]
    for i, (cat, total) in enumerate(ranked[:top_n], start=1):
        lines.append(f"{i}. «{cat}»: {total:g} {currency}")
    return "\n".join(lines)
