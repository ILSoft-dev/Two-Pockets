"""
Парсинг текстового ввода вида "кофе 150р", "зарплата 100000₽",
"2 мороженых по 4 рубля" (количество × цена, только с явным маркером "по").

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

# Количество × цена — срабатывает ТОЛЬКО с явным маркером "по" между
# количеством и ценой ("2 мороженых по 4 рубля"). Без "по" — никакого
# умножения, число из AMOUNT_PATTERN просто берётся как итог (безопасный
# фоллбэк вместо угадывания, если количество не обозначено явно).
# "item" — название товара между qty и "по" (нежадно, максимум 30 символов,
# чтобы не захватить случайно что-то далёкое по тексту) — ВАЖНО вернуть его
# в remainder отдельно, иначе категоризатору не на чём будет угадывать.
QUANTITY_PATTERN = re.compile(
    r"(?P<qty>\d+)\s*(?:шт\.?|штук\w*)?\s*"
    r"(?P<item>.{0,30}?)"
    r"\bпо\s+"
    r"(?P<unit_price>\d+(?:[.,]\d+)?)\s*"
    r"(?P<currency>руб\.?|р\.?|₽|usd|\$|eur|€|byn|br)",
    re.IGNORECASE | re.DOTALL,
)


def parse_amount(text: str):
    """
    Возвращает (amount: float, currency_code: str, remainder_text: str) или None,
    если валюта не указана явно (по требованию — без валюты не парсим).
    """
    qty_match = QUANTITY_PATTERN.search(text)
    if qty_match:
        qty = int(qty_match.group("qty"))
        unit_price = float(qty_match.group("unit_price").replace(",", "."))
        raw_currency = qty_match.group("currency").lower().replace(".", "")
        amount = qty * unit_price
        currency_code = CURRENCY_SYMBOLS.get(raw_currency, "RUB")
        item_text = qty_match.group("item").strip()
        remainder = (
            text[: qty_match.start()] + " " + item_text + " " + text[qty_match.end():]
        ).strip()
        remainder = re.sub(r"\s+", " ", remainder).strip()
        return amount, currency_code, remainder

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
