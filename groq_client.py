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
