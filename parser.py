"""
Парсинг текстового ввода вида "кофе 150р", "зарплата 100000₽".

Обязательное условие (см. README): пользователь ВСЕГДА указывает валюту
рядом с числом, иначе бот не может отличить сумму от количества/массы
("сахар 5" — 5 это кг или рубли?).
"""
import re
from config import INCOME_KEYWORDS, CURRENCY_SYMBOLS

# Матчит число (с точкой/запятой как разделителем дробной части) сразу
# перед или после символа валюты. Примеры, которые должны сработать:
# "150р", "150 р", "150₽", "$150", "100000руб", "12.5$"
AMOUNT_PATTERN = re.compile(
    r"(?P<amount>\d+(?:[.,]\d+)?)\s*(?P<currency>руб\.?|р\.?|₽|usd|\$|eur|€|byn|br)"
    r"|(?P<currency2>\$|€)\s*(?P<amount2>\d+(?:[.,]\d+)?)",
    re.IGNORECASE,
)


def parse_amount(text: str):
    """
    Возвращает (amount: float, currency_code: str, remainder_text: str) или None,
    если валюта не указана явно (по требованию — без валюты не парсим).
    """
    match = AMOUNT_PATTERN.search(text)
    if not match:
        return None

    if match.group("amount"):
        raw_amount = match.group("amount")
        raw_currency = match.group("currency").lower().replace(".", "")
    else:
        raw_amount = match.group("amount2")
        raw_currency = match.group("currency2").lower()

    amount = float(raw_amount.replace(",", "."))
    currency_code = CURRENCY_SYMBOLS.get(raw_currency, "RUB")

    # Остаток текста (без найденного числа+валюты) — сырьё для категории
    remainder = (text[: match.start()] + " " + text[match.end():]).strip()
    remainder = re.sub(r"\s+", " ", remainder).strip()

    return amount, currency_code, remainder


def guess_type(remainder_text: str) -> str:
    """'income' если встретилось ключевое слово дохода, иначе 'expense'."""
    lowered = remainder_text.lower()
    for kw in INCOME_KEYWORDS:
        if kw in lowered:
            return "income"
    return "expense"
