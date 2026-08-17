"""
Обёртка над Groq API:
- categorize_text  — LLM подбирает категорию из списка (fallback, если
  парсер по ключевым словам и category_map не справились)
- transcribe_voice  — Whisper large-v3, голос -> текст
- extract_receipt_total — Vision (qwen3.6-27b), фото чека -> сумма

Changelog:
- v1.1: TEXT_MODEL переключён на gpt-oss-20b (быстрее/дешевле, чем 120b,
        достаточно для тривиальной классификации в одно слово) +
        reasoning_effort="low" — gpt-oss — reasoning-модель, часть токенов
        по умолчанию уходит на внутреннее рассуждение ДО финального ответа,
        а старый max_tokens=20 (нормальный для прежней не-reasoning модели)
        мог обрезать ответ до того, как модель успевала написать саму
        категорию. Добавлено логирование сырого ответа для диагностики.
        requirements.txt: groq bumped 0.13.0 -> 1.6.0 — старая версия SDK
        вообще не знает про reasoning_effort (строго типизированный create(),
        без **kwargs) и упала бы с TypeError.
"""
import base64
import json
import logging
import re
from groq import Groq

from config import GROQ_API_KEY

client = Groq(api_key=GROQ_API_KEY)

# llama-4-scout-17b-16e-instruct отключена Groq 17 июня 2026 (см.
# console.groq.com/docs/deprecations). ВАЖНО: llama-3.3-70b-versatile тоже
# в процессе отключения — не откатываться туда. Рекомендованное направление
# Groq — gpt-oss (text) / qwen3.6-27b (vision).
TEXT_MODEL = "openai/gpt-oss-20b"
VISION_MODEL = "qwen/qwen3.6-27b"
WHISPER_MODEL = "whisper-large-v3"


def categorize_text(remainder_text: str, categories: list[str]) -> str:
    prompt = (
        f"Определи наиболее подходящую категорию из списка: {', '.join(categories)}.\n"
        f"Текст траты/дохода: \"{remainder_text}\"\n"
        f"Ответь ТОЛЬКО названием категории из списка, без пояснений."
    )
    completion = client.chat.completions.create(
        model=TEXT_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        max_tokens=200,          # запас для reasoning-модели (gpt-oss)
        reasoning_effort="low",  # не нужны развёрнутые рассуждения на классификацию в одно слово
    )
    answer = (completion.choices[0].message.content or "").strip()
    logging.info(f"categorize_text: remainder={remainder_text!r} raw_answer={answer!r}")

    # Подстраховка: если модель вернула что-то не из списка — берём "Разное"
    for cat in categories:
        if cat.lower() in answer.lower():
            return cat
    return "Разное"


def transcribe_voice(audio_bytes: bytes, filename: str = "voice.ogg") -> str:
    transcription = client.audio.transcriptions.create(
        file=(filename, audio_bytes),
        model=WHISPER_MODEL,
        language="ru",
    )
    return transcription.text.strip()


def parse_question(text: str, categories: list[str], car_names: list[str],
                   previous_context: dict | None = None) -> dict | None:
    """Разбирает вопрос типа 'сколько я потратил на корм в июне?' в структуру:
    {"intent": "spending"|"income"|"mileage"|"compare_periods"|"top_category"|"unknown",
     "answer_type": "sum"|"when"|"count"|"average",
     "category": <строка из categories или null>,
     "item": <конкретный товар/продукт из вопроса, если назван, иначе null>,
     "car_name": <строка из car_names или null>,
     "period_type": "specific_month"|"current_period"|"all_time",
     "month": <1-12 или null>, "year": <год или null>}

    "previous_context" — разбор предыдущего вопроса ЭТОГО ЖЕ пользователя
    (если был в последние 10 минут, см. insights.py) — для уточняющих
    вопросов вроде "а в июле?" после "сколько на продукты в июне?". LLM
    сама решает, унаследовать ли поля из контекста (уточнение) или
    разобрать вопрос с нуля (самостоятельный, не связанный вопрос).

    "intent=compare_periods" — "в этом месяце потратил больше, чем в
    прошлом?". period_type/month/year описывают ОДИН (первый упомянутый)
    период — наш код сам вычисляет "предыдущий период той же длины" и
    сравнивает, а не просит LLM угадывать даты второго периода (не
    доверяем модели арифметику с датами).

    "intent=top_category" — "какая моя самая большая категория трат в этом
    году?" — period_type/month/year как обычно описывают период анализа.

    "answer_type=count" — "сколько раз я покупал кофе?" (количество записей).
    "answer_type=average" — "сколько в среднем трачу на еду?" (среднее в месяц).

    "answer_type": "when" — для "когда я покупал подписку на Claude?": нужна
    не сумма, а дата(ы) покупки. Период по умолчанию для "when" — all_time
    (не current_period, как для сумм) — если человек спрашивает "когда
    покупал", он обычно НЕ помнит период, иначе бы и не спрашивал.

    "item" — для случаев вроде "сколько потратил на мороженое?": мороженое
    не входит в список категорий пользователя (только "Продукты" и т.п.),
    и раньше вопрос молча сводился к сумме по всей угаданной категории, а
    не по конкретному товару. Теперь LLM явно отделяет "товар" от
    "категория" — дальше наш код ищет "item" подстрокой в описании
    транзакции, а не в названии категории. "item" и "car_name" МОГУТ быть
    заполнены оба одновременно ("сколько на бензин для опеля" —
    item="бензин", car_name="Опель") — тогда код ищет именно в авто-листе
    по этой машине, а не в общем списке трат.

    НЕ используем response_format ни в каком виде — у gpt-oss на Groq была
    подтверждённая проблема с игнорированием json_schema (модель тихо
    возвращает свободный текст), и надёжность даже json_object под вопросом.
    Вместо этого — явная схема прямо в промпте + защитный разбор ответа.
    Возвращает None при любом сбое (сеть, парсинг) — вызывающий код должен
    вежливо ответить "не понял вопрос", а не падать.
    """
    schema_hint = (
        '{"intent": "spending" | "income" | "mileage" | "compare_periods" | '
        '"top_category" | "unknown", '
        '"answer_type": "sum" | "when" | "count" | "average", '
        '"category": "<строка из известных категорий или null>", '
        '"item": "<конкретный товар/продукт, если назван явно, иначе null>", '
        '"car_name": "<строка из известных машин или null>", '
        '"period_type": "specific_month" | "current_period" | "all_time", '
        '"month": <число 1-12 или null>, "year": <число или null>}'
    )
    known = (
        f"Известные категории: {', '.join(categories) if categories else 'нет'}.\n"
        f"Известные машины: {', '.join(car_names) if car_names else 'нет'}."
    )
    context_hint = ""
    if previous_context:
        context_hint = (
            f"\nПредыдущий вопрос этого же пользователя был разобран так: "
            f"{json.dumps(previous_context, ensure_ascii=False)}\n"
            "Если новый вопрос — уточнение/продолжение предыдущего (например, "
            "меняет только период, но явно про ту же тему/товар/категорию/машину) "
            "— возьми из предыдущего разбора то, что НЕ упомянуто явно в новом "
            "вопросе. Если новый вопрос самостоятельный и не связан с предыдущим "
            "— полностью игнорируй этот контекст, разбери с нуля.\n"
        )
    prompt = (
        f"Вопрос пользователя: \"{text}\"\n{known}\n{context_hint}\n"
        f"Разбери вопрос строго в JSON по этой схеме, без пояснений и markdown:\n{schema_hint}\n\n"
        "Правила:\n"
        "- intent=mileage только если явно спрашивают про пробег/километраж.\n"
        "- intent=spending для трат/расходов, intent=income для доходов/зарплаты.\n"
        "- intent=compare_periods, если явно сравнивают два периода "
        "(\"больше/меньше, чем в прошлом месяце/году\", \"вырос ли\").\n"
        "- intent=top_category, если спрашивают про САМУЮ большую/маленькую "
        "категорию трат за период, а не про конкретную категорию/товар.\n"
        "- answer_type=when, если спрашивают КОГДА произошла покупка/операция "
        "(\"когда я покупал X\", \"когда в последний раз платил за Y\"), а не сколько.\n"
        "- answer_type=count, если спрашивают СКОЛЬКО РАЗ/сколько записей "
        "(\"сколько раз покупал X\"), а не сумму.\n"
        "- answer_type=average, если спрашивают про СРЕДНЕЕ значение "
        "(\"сколько в среднем трачу на X\").\n"
        "- answer_type=sum для всех остальных вопросов про суммы (по умолчанию).\n"
        "- category — ТОЛЬКО точное совпадение из списка известных категорий, иначе null.\n"
        "- item — если вопрос про КОНКРЕТНЫЙ товар/продукт, которого нет в списке "
        "известных категорий (например \"мороженое\", \"хлеб\", \"корм для кота\", "
        "\"подписка на Claude\", \"бензин\") — положи его сюда в исходной форме из "
        "вопроса, а category оставь null, если товар явно не совпадает ни с одной "
        "категорией. Если товар и категория упомянуты оба — заполни оба поля.\n"
        "- car_name — ТОЛЬКО точное совпадение из списка известных машин, иначе "
        "null. Заполняй его ВСЕГДА, когда машина названа явно, независимо от "
        "intent — и для трат (\"сколько на бензин для опеля\"), и для пробега.\n"
        "- period_type=specific_month, если назван конкретный месяц (год может быть не назван).\n"
        "- period_type=current_period, если период не назван вообще И answer_type=sum "
        "(спрашивают \"сколько я потратил\" без уточнения когда).\n"
        "- period_type=all_time, если период не назван вообще И answer_type=when "
        "(человек не помнит период — иначе бы не спрашивал \"когда\"), а также если "
        "период явно назван как \"за всё время\"/\"всего\"/\"с начала\".\n"
        "- month — номер месяца 1-12, если назван (иначе null). year — если назван явно (иначе null)."
    )
    try:
        completion = client.chat.completions.create(
            model=TEXT_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=300,
            reasoning_effort="low",
        )
        answer = (completion.choices[0].message.content or "").strip()
    except Exception:
        logging.exception("parse_question: Groq request failed")
        return None

    logging.info(f"parse_question: text={text!r} raw_answer={answer!r}")

    # Защитный разбор: снимаем возможные markdown-обёртки ```json ... ```,
    # берём первый {...} блок, а не доверяем, что весь ответ — чистый JSON.
    cleaned = answer.replace("```json", "").replace("```", "").strip()
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not match:
        logging.warning(f"parse_question: no JSON object found in answer: {answer!r}")
        return None
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        logging.warning(f"parse_question: failed to decode JSON: {match.group(0)!r}")
        return None

    if not isinstance(parsed, dict) or "intent" not in parsed:
        return None
    return parsed


def extract_receipt_total(image_bytes: bytes) -> float | None:
    b64_image = base64.b64encode(image_bytes).decode("utf-8")
    prompt = (
        "На фото чек из магазина или кафе. Найди итоговую сумму покупки "
        "(строка 'Итого' / 'К оплате' / 'Сумма'). "
        "Ответь СТРОГО в формате: СУММА: <число без валюты и пробелов>. "
        "Если не удаётся распознать сумму, ответь: СУММА: НЕТ"
    )
    completion = client.chat.completions.create(
        model=VISION_MODEL,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64_image}"},
                    },
                ],
            }
        ],
        temperature=0,
        max_tokens=60,
    )
    answer = (completion.choices[0].message.content or "").strip()
    logging.info(f"extract_receipt_total: raw_answer={answer!r}")

    match = re.search(r"(\d+(?:[.,]\d+)?)", answer)
    if not match or "НЕТ" in answer.upper():
        return None
    return float(match.group(1).replace(",", "."))
