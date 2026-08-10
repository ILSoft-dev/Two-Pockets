from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext

import supabase_client as db
import pin_handler
from states import SettingsStates
from keyboards import settings_menu_keyboard, currency_keyboard

router = Router()


@router.message(Command("settings"))
async def cmd_settings(message: Message):
    user = db.get_user(message.from_user.id)
    if not user or not user.get("onboarding_done"):
        await message.answer("Сначала пройди настройку: /start")
        return

    await message.answer(
        f"⚙️ <b>Настройки</b>\n"
        f"Валюта: {user['currency']}\n"
        f"Начало периода: {user['month_start']} число\n"
        f"PIN: {'установлен' if user.get('pin_hash') else 'не установлен'}",
        reply_markup=settings_menu_keyboard(bool(user.get("pin_hash"))),
    )


@router.callback_query(F.data == "settings:currency")
async def settings_currency(callback: CallbackQuery, state: FSMContext):
    await state.set_state(SettingsStates.waiting_currency)
    await callback.message.answer("Выбери валюту:", reply_markup=currency_keyboard())
    await callback.answer()


@router.callback_query(SettingsStates.waiting_currency, F.data.startswith("currency:"))
async def settings_currency_set(callback: CallbackQuery, state: FSMContext):
    user = db.get_user(callback.from_user.id)
    currency = callback.data.split(":")[1]
    db.update_user(user["id"], currency=currency)
    await state.clear()
    await callback.message.answer(f"✅ Валюта изменена на {currency}.")
    await callback.answer()


@router.callback_query(F.data == "settings:period")
async def settings_period(callback: CallbackQuery, state: FSMContext):
    await state.set_state(SettingsStates.waiting_month_start)
    await callback.message.answer("Введи новый день начала отчётного периода (1-28):")
    await callback.answer()


@router.message(SettingsStates.waiting_month_start)
async def settings_period_set(message: Message, state: FSMContext):
    text = message.text.strip()
    if not text.isdigit() or not (1 <= int(text) <= 28):
        await message.answer("Нужно число от 1 до 28.")
        return
    user = db.get_user(message.from_user.id)
    db.update_user(user["id"], month_start=int(text))
    await state.clear()
    await message.answer(f"✅ Начало периода: {text} число.")


@router.callback_query(F.data == "settings:pin")
async def settings_pin_toggle(callback: CallbackQuery, state: FSMContext):
    user = db.get_user(callback.from_user.id)
    if user.get("pin_hash"):
        db.set_pin(user["id"], None)
        await callback.message.answer("🔓 PIN отключён.")
        await callback.answer()
        return

    await state.set_state(SettingsStates.waiting_pin_new)
    await callback.message.answer("Придумай 4-значный PIN:")
    await callback.answer()


@router.message(SettingsStates.waiting_pin_new)
async def settings_pin_new(message: Message, state: FSMContext):
    pin = message.text.strip()
    if not (pin.isdigit() and len(pin) == 4):
        await message.answer("PIN должен состоять ровно из 4 цифр.")
        return
    await state.update_data(pending_pin=pin)
    await state.set_state(SettingsStates.waiting_pin_confirm)
    await message.answer("Повтори PIN для подтверждения:")


@router.message(SettingsStates.waiting_pin_confirm)
async def settings_pin_confirm(message: Message, state: FSMContext):
    data = await state.get_data()
    if message.text.strip() != data.get("pending_pin"):
        await state.set_state(SettingsStates.waiting_pin_new)
        await message.answer("Не совпадает. Введи PIN заново:")
        return

    user = db.get_user(message.from_user.id)
    db.set_pin(user["id"], pin_handler.hash_pin(message.text.strip()))
    await pin_handler.open_session(message.from_user.id)
    await state.clear()
    await message.answer("🔐 PIN установлен.")
