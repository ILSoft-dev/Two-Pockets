"""
input_handler.py
v2.0 - text/voice/photo expense input, now backed by Google Sheets

Changelog:
- v2.0: db.add_transaction() (Supabase) replaced with sheets_transactions
        (Google Sheets, effective account resolved via family ownership).
        Category "Авто" gets structured parsing (car/type/mileage) via
        cars.match_car_name + auto_expense heuristics, with a car-choice
        disambiguation flow when the car can't be determined automatically.
        Standalone mileage updates ("пробег опель 305000 км" — no currency,
        so parse_amount would otherwise reject them) get a dedicated branch
        that shares the same car-disambiguation flow.
"""
import re

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, ReactionTypeEmoji, User
from aiogram.fsm.context import FSMContext

import supabase_client as db
import sheets_transactions as tx
import groq_client
import cars
import auto_expense
import fluid_tracker
from parser import parse_amount, guess_type
from keyboards import category_choice_keyboard, car_choice_keyboard
from states import AmbiguousCategoryStates, CarResolutionStates

router = Router()

INCOME_CATEGORIES = ["Зарплата", "Подработка"]
AUTO_CATEGORY = "Авто"


def who_label(user: User) -> str:
    """Attribution for the 'Кто' column — matters mainly in a shared family
    sheet, where transactions from both partners land in one place."""
    return user.username or user.first_name or str(user.id)


async def react_ok(message: Message):
    try:
        await message.bot.set_message_reaction(
            chat_id=message.chat.id,
            message_id=message.message_id,
            reaction=[ReactionTypeEmoji(emoji="👍")],
        )
    except Exception:
        # Реакции могут быть недоступны в некоторых чатах — не критично
        await message.answer("✅ Записано")


def first_keyword(remainder: str) -> str:
    words = remainder.strip().split()
    return words[0].lower() if words else "разное"


async def resolve_expense_category(user_id: int, remainder: str) -> tuple[str, bool]:
    """Возвращает (категория, ambiguous). ambiguous=True — нужно спросить юзера."""
    kw_category = db.lookup_keyword_category(user_id, remainder)
    if kw_category:
        return kw_category, False

    categories = [c["name"] for c in db.get_categories(user_id)]
    guessed = groq_client.categorize_text(remainder, categories)
    return guessed, guessed == "Разное"


def resolve_income_category(remainder: str) -> tuple[str, bool]:
    lowered = remainder.lower()
    if "зарплат" in lowered or " зп" in f" {lowered}":
        return "Зарплата", False
    if "подработ" in lowered:
        return "Подработка", False
    return "Разное", True


# ------------------------------------------------------------- saving ------
async def save_and_confirm(message: Message, user_id: int, who: str, amount: float,
                           tx_type: str, category: str, source: str, comment: str = ""):
    try:
        await tx.save_transaction(user_id, who, amount, tx_type, category, source, comment)
    except tx.NoGoogleAccount:
        await message.answer("Google Drive не подключён — пройди заново /start, чтобы подключить.")
        return
    await react_ok(message)


async def finalize_auto_expense(message: Message, user_id: int, who: str, amount: float,
                                tx_type: str, car_name: str, auto_type: str,
                                description: str, mileage: float | None, source: str):
    try:
        await tx.save_auto_expense(user_id, who, amount, tx_type, car_name, auto_type,
                                   description, mileage, source)
    except tx.NoGoogleAccount:
        await message.answer("Google Drive не подключён — пройди заново /start, чтобы подключить.")
        return
    await react_ok(message)
    if mileage is not None:
        await maybe_warn_fluids(message, user_id, car_name, mileage)


async def ask_car_disambiguation(message: Message, state: FSMContext, active_cars: list[dict]):
    if active_cars:
        await state.set_state(CarResolutionStates.waiting_car_choice)
        await message.answer("Это про какую машину?", reply_markup=car_choice_keyboard(active_cars))
    else:
        await state.set_state(CarResolutionStates.waiting_new_car_name)
        await message.answer("У тебя пока нет зарегистрированных машин. Как назвать эту?")


async def route_auto_expense(message: Message, state: FSMContext, user_id: int, who: str,
                             amount: float, tx_type: str, source: str, remainder: str):
    account = db.get_effective_google_account(user_id)
    active_cars = (
        await cars.list_active_cars(account["google_access_token"], account["google_spreadsheet_id"])
        if account else []
    )

    matched_name = cars.match_car_name(remainder, active_cars)
    mileage = auto_expense.extract_mileage(remainder)
    auto_type = auto_expense.classify_auto_type(remainder)

    if matched_name:
        await finalize_auto_expense(message, user_id, who, amount, tx_type, matched_name,
                                    auto_type, remainder, mileage, source)
        await state.clear()
        return

    if len(active_cars) == 1:
        await finalize_auto_expense(message, user_id, who, amount, tx_type, active_cars[0]["Машина"],
                                    auto_type, remainder, mileage, source)
        await state.clear()
        return

    await state.update_data(pending_intent="auto_expense", pending_payload={
        "amount": amount, "tx_type": tx_type, "auto_type": auto_type,
        "description": remainder, "mileage": mileage, "source": source,
    })
    await ask_car_disambiguation(message, state, active_cars)


async def handle_mileage_message(message: Message, state: FSMContext, user_id: int,
                                 who: str, text: str):
    leftover, mileage = cars.parse_mileage_message(text)
    if mileage is None:
        await message.answer(
            "Не вижу валюту рядом с числом, и не смог понять пробег 🤔\n"
            "Если это трата — укажи валюту («кофе 150р»). Если хочешь "
            "обновить пробег — напиши, например, «пробег опель 305000 км»."
        )
        return

    account = db.get_effective_google_account(user_id)
    active_cars = (
        await cars.list_active_cars(account["google_access_token"], account["google_spreadsheet_id"])
        if account else []
    )
    matched_name = cars.match_car_name(leftover, active_cars)

    if matched_name:
        await save_mileage_and_confirm(message, user_id, who, matched_name, mileage)
        return
    if len(active_cars) == 1:
        await save_mileage_and_confirm(message, user_id, who, active_cars[0]["Машина"], mileage)
        return

    await state.update_data(pending_intent="mileage_update", pending_payload={"mileage": mileage})
    await ask_car_disambiguation(message, state, active_cars)


async def maybe_warn_fluids(message: Message, user_id: int, car_name: str, mileage: float):
    """Called after every fresh mileage point (standalone update, reminder
    'без изменений', or a repair message that mentioned mileage) — checks
    whether any tracked fluid is due soon and warns if so."""
    account = db.get_effective_google_account(user_id)
    if not account:
        return
    due = await fluid_tracker.check_due_fluids(
        account["google_access_token"], account["google_spreadsheet_id"], car_name, mileage
    )
    if due:
        await message.answer(fluid_tracker.format_due_warning(due, car_name))


async def save_mileage_and_confirm(message: Message, user_id: int, who: str,
                                   car_name: str, mileage: float):
    try:
        await tx.save_mileage_point(user_id, who, car_name, mileage)
    except tx.NoGoogleAccount:
        await message.answer("Google Drive не подключён — пройди заново /start, чтобы подключить.")
        return
    await message.answer(f"Записал пробег «{car_name}»: {mileage:g} км")
    await maybe_warn_fluids(message, user_id, car_name, mileage)


@router.callback_query(F.data.startswith("mileage_same:"))
async def mileage_unchanged(callback: CallbackQuery):
    """'Без изменений' на еженедельном напоминании (reminders.py) — не
    просто игнорируем, а честно логируем точку с тем же пробегом на
    сегодняшнюю дату, чтобы средний км/месяц учитывал реальный простой."""
    car_id = callback.data.split(":", 1)[1]
    user = db.get_or_create_user(callback.from_user.id, callback.from_user.username)
    who = who_label(callback.from_user)
    account = db.get_effective_google_account(user["id"])

    if not account:
        await callback.message.answer("Google Drive не подключён — пройди заново /start.")
        await callback.answer()
        return

    active_cars = await cars.list_active_cars(account["google_access_token"], account["google_spreadsheet_id"])
    car_row = next((c for c in active_cars if c["ID"] == car_id), None)
    if not car_row:
        await callback.message.edit_text("Не нашёл эту машину — возможно, её уже удалили.")
        await callback.answer()
        return

    last_mileage = await cars.get_latest_mileage(
        account["google_access_token"], account["google_spreadsheet_id"], car_row["Машина"]
    )
    if last_mileage is None:
        await callback.message.edit_text(
            f"Нет предыдущих записей пробега для «{car_row['Машина']}» — напиши пробег вручную."
        )
        await callback.answer()
        return

    await tx.save_mileage_point(user["id"], who, car_row["Машина"], last_mileage, source="Без изменений")
    await callback.message.edit_text(f"Записал: «{car_row['Машина']}» без изменений ({last_mileage:g} км)")
    await maybe_warn_fluids(callback.message, user["id"], car_row["Машина"], last_mileage)
    await callback.answer()


# ------------------------------------------------------- car disambiguation --
@router.callback_query(CarResolutionStates.waiting_car_choice, F.data.startswith("car_choice:"))
async def car_choice_picked(callback: CallbackQuery, state: FSMContext):
    car_id = callback.data.split(":", 1)[1]
    user = db.get_or_create_user(callback.from_user.id, callback.from_user.username)
    account = db.get_effective_google_account(user["id"])
    active_cars = (
        await cars.list_active_cars(account["google_access_token"], account["google_spreadsheet_id"])
        if account else []
    )
    car_row = next((c for c in active_cars if c["ID"] == car_id), None)
    car_name = car_row["Машина"] if car_row else "?"

    await resolve_pending_intent(callback.message, state, user["id"], callback.from_user, car_name)
    await callback.answer()


@router.callback_query(CarResolutionStates.waiting_car_choice, F.data == "car_choice_new")
async def car_choice_new(callback: CallbackQuery, state: FSMContext):
    await state.set_state(CarResolutionStates.waiting_new_car_name)
    await callback.message.answer("Как назвать машину?")
    await callback.answer()


@router.message(CarResolutionStates.waiting_new_car_name)
async def new_car_named(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.answer("Напиши название машины текстом.")
        return

    user = db.get_or_create_user(message.from_user.id, message.from_user.username)
    who = who_label(message.from_user)
    account = db.get_effective_google_account(user["id"])
    if account:
        await cars.add_car(account["google_access_token"], account["google_spreadsheet_id"], name, who=who)

    await resolve_pending_intent(message, state, user["id"], message.from_user, name)


async def resolve_pending_intent(message: Message, state: FSMContext, user_id: int,
                                 from_user: User, car_name: str):
    data = await state.get_data()
    who = who_label(from_user)
    intent = data.get("pending_intent")
    payload = data.get("pending_payload", {})

    if intent == "auto_expense":
        await finalize_auto_expense(
            message, user_id, who, payload["amount"], payload["tx_type"], car_name,
            payload["auto_type"], payload["description"], payload["mileage"], payload["source"],
        )
    elif intent == "mileage_update":
        await save_mileage_and_confirm(message, user_id, who, car_name, payload["mileage"])

    await state.clear()


# --------------------------------------------------------- ambiguous category-
async def ask_category_choice(
    message: Message, state: FSMContext, amount: float, tx_type: str, source: str, remainder: str
):
    user = db.get_user(message.from_user.id) or db.get_or_create_user(
        message.from_user.id, message.from_user.username
    )
    categories = [c["name"] for c in db.get_categories(user["id"])]
    if tx_type == "income":
        categories = INCOME_CATEGORIES + [c for c in categories if c not in INCOME_CATEGORIES]

    # Храним ПОЛНЫЙ remainder, а не только первое слово — если в итоге
    # выберут "Авто", нужен весь текст для разбора машины/типа/пробега.
    await state.update_data(amount=amount, tx_type=tx_type, source=source, remainder=remainder)
    await state.set_state(AmbiguousCategoryStates.waiting_choice)
    await message.answer(
        f"Не уверен насчёт категории для {amount:g}. Выбери подходящую:",
        reply_markup=category_choice_keyboard(categories),
    )


@router.callback_query(AmbiguousCategoryStates.waiting_choice, F.data.startswith("cat_choice:"))
async def category_chosen(callback: CallbackQuery, state: FSMContext):
    category = callback.data.split(":", 1)[1]
    data = await state.get_data()
    user = db.get_or_create_user(callback.from_user.id, callback.from_user.username)
    who = who_label(callback.from_user)
    remainder = data.get("remainder", "")

    if category == AUTO_CATEGORY:
        await route_auto_expense(
            callback.message, state, user["id"], who,
            data["amount"], data["tx_type"], data["source"], remainder,
        )
        # route_auto_expense сам решает, чистить ли state (может понадобиться
        # дизамбигуация машины — тогда state переходит в CarResolutionStates)
        await callback.answer()
        return

    await save_and_confirm(callback.message, user["id"], who, data["amount"], data["tx_type"],
                           category, data["source"], comment=remainder)
    keyword = first_keyword(remainder)
    if keyword:
        db.remember_keyword_category(user["id"], keyword, category)

    await state.clear()
    await callback.answer()


# ------------------------------------------------------------ media intake ---
async def process_text_input(message: Message, state: FSMContext, text: str, source: str):
    user = db.get_or_create_user(message.from_user.id, message.from_user.username)
    if not user.get("onboarding_done"):
        await message.answer("Сначала пройди короткую настройку: /start")
        return

    who = who_label(message.from_user)
    parsed = parse_amount(text)

    if parsed is None:
        # Отдельная ветка: "пробег опель 305000 км" — валюты в таком
        # сообщении нет и не будет, это не трата, а обновление пробега.
        if re.search(r"(?i)пробег", text):
            await handle_mileage_message(message, state, user["id"], who, text)
            return
        await message.answer(
            "Не вижу валюту рядом с числом 🤔\n"
            "Указывай так: «кофе 150р», «зарплата 100000₽» — иначе не могу "
            "отличить сумму от количества/массы."
        )
        return

    amount, _currency_code, remainder = parsed
    tx_type = guess_type(remainder)

    if tx_type == "income":
        category, ambiguous = resolve_income_category(remainder)
    else:
        category, ambiguous = await resolve_expense_category(user["id"], remainder)

    if ambiguous:
        await ask_category_choice(message, state, amount, tx_type, source, remainder)
        return

    if category == AUTO_CATEGORY:
        await route_auto_expense(message, state, user["id"], who, amount, tx_type, source, remainder)
        return

    await save_and_confirm(message, user["id"], who, amount, tx_type, category, source, comment=remainder)


@router.message(F.voice)
async def handle_voice(message: Message, state: FSMContext):
    file = await message.bot.get_file(message.voice.file_id)
    file_bytes = await message.bot.download_file(file.file_path)
    text = groq_client.transcribe_voice(file_bytes.read(), filename="voice.ogg")
    if not text:
        await message.answer("Не удалось распознать голос, попробуй ещё раз.")
        return
    await process_text_input(message, state, text, source="voice")


@router.message(F.photo)
async def handle_receipt_photo(message: Message, state: FSMContext):
    user = db.get_or_create_user(message.from_user.id, message.from_user.username)
    if not user.get("onboarding_done"):
        await message.answer("Сначала пройди короткую настройку: /start")
        return

    largest_photo = message.photo[-1]
    file = await message.bot.get_file(largest_photo.file_id)
    file_bytes = await message.bot.download_file(file.file_path)
    total = groq_client.extract_receipt_total(file_bytes.read())

    if total is None:
        await message.answer("Не смог распознать сумму на чеке. Попробуй сфотографировать чётче.")
        return

    await ask_category_choice(message, state, total, "expense", "receipt", remainder="")


# Этот хендлер должен регистрироваться ПОСЛЕДНИМ в диспетчере (после команд и FSM-специфичных
# хендлеров), чтобы не перехватывать текст, относящийся к онбордингу/настройкам/т.д.
@router.message(F.text)
async def handle_text(message: Message, state: FSMContext):
    await process_text_input(message, state, message.text, source="text")
