"""
sheets_transactions.py
v1.0 - transactions CRUD backed by Google Sheets (replaces Supabase transactions table)

All money data now lives in the *effective* Google account's spreadsheet
(supabase_client.get_effective_google_account — own account, or the family
owner's if in a family). Supabase itself only holds metadata (users,
categories, category_map, family graph, PIN) — see schema.sql.
"""
from datetime import datetime, timedelta, timezone

import aiohttp

import supabase_client as db
import sheets_client as sc
from sheets_client import to_float  # re-exported: history.py/undo.py use tx.to_float

STATUS_ACTIVE = "Активна"
STATUS_DELETED = "Удалена"


class NoGoogleAccount(Exception):
    """Raised when the effective account has no Google Drive connected.
    Callers should tell the user to /start (or reconnect), not crash."""


def _get_account(user_id: int) -> dict:
    account = db.get_effective_google_account(user_id)
    if not account:
        raise NoGoogleAccount()
    return account


# --------------------------------------------------------------- writing ----
async def save_transaction(user_id: int, who: str, amount: float, tx_type: str,
                           category: str, source: str, comment: str = "") -> str:
    account = _get_account(user_id)
    async with aiohttp.ClientSession() as session:
        return await sc.append_row(
            session, account["google_access_token"], account["google_spreadsheet_id"],
            sc.SHEET_TRANSACTIONS,
            [sc.now_iso(), who, tx_type, category, amount, source, comment, STATUS_ACTIVE],
        )


async def save_auto_expense(user_id: int, who: str, amount: float, tx_type: str,
                            car_name: str, auto_type: str, description: str,
                            mileage: float | None, source: str) -> tuple[str, str]:
    """Writes both the general Транзакции row (so /report totals include
    it like any other expense) and the structured Авто row. Also logs a
    mileage point if one was mentioned in the message. Returns
    (transactions_row_id, auto_row_id)."""
    account = _get_account(user_id)
    async with aiohttp.ClientSession() as session:
        tx_id = await sc.append_row(
            session, account["google_access_token"], account["google_spreadsheet_id"],
            sc.SHEET_TRANSACTIONS,
            [sc.now_iso(), who, tx_type, "Авто", amount, source, description, STATUS_ACTIVE],
        )
        auto_id = await sc.append_row(
            session, account["google_access_token"], account["google_spreadsheet_id"],
            sc.SHEET_AUTO,
            [sc.now_iso(), car_name, auto_type, description, amount, mileage or "", who, STATUS_ACTIVE],
        )
        if mileage is not None:
            await sc.append_row(
                session, account["google_access_token"], account["google_spreadsheet_id"],
                sc.SHEET_MILEAGE,
                [sc.now_iso(), car_name, mileage, "Из авто-траты", who],
            )
    return tx_id, auto_id


async def save_mileage_point(user_id: int, who: str, car_name: str, mileage: float,
                             source: str = "Ручной ввод") -> str:
    account = _get_account(user_id)
    async with aiohttp.ClientSession() as session:
        return await sc.append_row(
            session, account["google_access_token"], account["google_spreadsheet_id"],
            sc.SHEET_MILEAGE,
            [sc.now_iso(), car_name, mileage, source, who],
        )


# --------------------------------------------------------------- reading ----
async def get_transactions_since(user_id: int, since: datetime) -> list[dict]:
    account = _get_account(user_id)
    async with aiohttp.ClientSession() as session:
        rows = await sc.get_rows(
            session, account["google_access_token"], account["google_spreadsheet_id"],
            sc.SHEET_TRANSACTIONS,
        )
    result = []
    for r in rows:
        if r["Статус"] != STATUS_ACTIVE:
            continue
        try:
            dt = datetime.fromisoformat(str(r["Дата и время"]))
        except ValueError:
            continue
        if dt >= since:
            result.append(r)
    result.sort(key=lambda r: r["Дата и время"], reverse=True)
    return result


async def get_report(user_id: int, since: datetime) -> dict:
    rows = await get_transactions_since(user_id, since)
    income = sum(to_float(r["Сумма"]) for r in rows if r["Тип"] == "income")
    expense = sum(to_float(r["Сумма"]) for r in rows if r["Тип"] == "expense")

    by_category: dict[str, float] = {}
    for r in rows:
        if r["Тип"] == "expense":
            by_category[r["Категория"]] = by_category.get(r["Категория"], 0) + to_float(r["Сумма"])
    top5 = sorted(by_category.items(), key=lambda x: x[1], reverse=True)[:5]

    return {"income": income, "expense": expense, "balance": income - expense, "top5": top5}


async def get_history(user_id: int, days: int) -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    return await get_transactions_since(user_id, since)


# --------------------------------------------------------------- /undo -------
async def soft_delete_last(user_id: int, who: str) -> dict | None:
    """Finds the most recent ACTIVE transaction attributed to `who` (not
    just anyone in a shared family sheet) and marks it Удалена. Returns the
    deleted row for the confirmation message, or None if nothing to undo."""
    account = _get_account(user_id)
    async with aiohttp.ClientSession() as session:
        rows = await sc.get_rows(
            session, account["google_access_token"], account["google_spreadsheet_id"],
            sc.SHEET_TRANSACTIONS,
        )
        candidates = [r for r in rows if r["Статус"] == STATUS_ACTIVE and r["Кто"] == who]
        if not candidates:
            return None
        candidates.sort(key=lambda r: r["Дата и время"], reverse=True)
        last = candidates[0]
        await sc.update_cell(
            session, account["google_access_token"], account["google_spreadsheet_id"],
            sc.SHEET_TRANSACTIONS, last["ID"], "Статус", STATUS_DELETED,
        )
    return last
