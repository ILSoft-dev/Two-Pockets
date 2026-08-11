import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
PORT = int(os.getenv("PORT", "8080"))

# Google OAuth — тот же Client ID/Secret, что уже настроен для PixKeep
# (Google Cloud проект + OAuth-клиент общие, это разные приложения на одном
# клиенте, не наоборот). Подтверждено: drive.file покрывает ВСЕ нужные
# Sheets API методы (create/batchUpdate/values.append/values.batchUpdate) —
# никакого Sensitive-скоупа "spreadsheets" не требуется.
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
# Должен точно совпадать с Authorized redirect URI в Google Cloud Console,
# напр. https://<этот-render-домен>.onrender.com/oauth/callback — ОТДЕЛЬНЫЙ
# от PixKeep-бота URI, т.к. это другой Render-сервис с другим доменом.
GOOGLE_OAUTH_REDIRECT_URI = os.getenv("GOOGLE_OAUTH_REDIRECT_URI")
GOOGLE_SCOPE = (
    "https://www.googleapis.com/auth/drive.file "
    "https://www.googleapis.com/auth/userinfo.email"
)

# Простой shared-secret для /cron/* эндпоинтов — без него кто угодно, кто
# найдёт URL, мог бы дёргать напоминания вручную сколько угодно раз.
CRON_SECRET = os.getenv("CRON_SECRET")

# Как часто изнутри бота (не через внешний cron) стучаться в Supabase, чтобы
# free-tier проект не приостановился из-за отсутствия активности (~7 дней
# без обращений к API). Дефолт — раз в 6 часов, большой запас на всякий случай.
SUPABASE_KEEPALIVE_INTERVAL_SECONDS = int(os.getenv("SUPABASE_KEEPALIVE_INTERVAL_SECONDS", str(6 * 60 * 60)))

# Ссылка на инструкцию (Telegraph). Можно поменять через переменную
# окружения без передеплоя кода, если гайд будет переопубликован по новому URL.
GUIDE_URL = os.getenv("GUIDE_URL", "https://telegra.ph/Instrukciya-k-Two-Pockets-bot-08-11")

# Дефолтные категории, создаются каждому юзеру при онбординге
DEFAULT_CATEGORIES = [
    "Продукты",
    "Транспорт",
    "Авто",
    "Досуг",
    "Коммуналка",
    "Зарплата",
    "Подработка",
    "Разное",
]

# Ключевые слова -> тип транзакции (Доход), всё остальное = Расход
INCOME_KEYWORDS = [
    "зарплата", "зп", "получил", "получила", "кэшбэк", "кешбек",
    "возврат", "подработка", "премия", "аванс", "перевод от",
]

# Символы валют, которые парсер распознаёт как признак суммы
CURRENCY_SYMBOLS = {
    "byn": "BYN", "br": "BYN",
    "р": "RUB", "руб": "RUB", "₽": "RUB",
    "$": "USD", "usd": "USD",
    "€": "EUR", "eur": "EUR",
}

PIN_SESSION_TTL_SECONDS = 300  # 5 минут
PIN_MAX_ATTEMPTS = 3
PIN_LOCKOUT_SECONDS = 300
