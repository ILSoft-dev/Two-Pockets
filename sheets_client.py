"""
sheets_client.py
v1.0 - Google Sheets storage layer
(async, aiohttp, drive.file scope only)
Replaces Supabase for the actual money data (transactions, car expenses,
mileage history, car registry).
Supabase stays for metadata that isn't financial:
users/onboarding, categories, category_map, family graph, PIN.
"""

import asyncio
import uuid
from datetime import datetime, timezone

import aiohttp

API = "https://sheets.googleapis.com/v4/spreadsheets"
DRIVE_API = "https://www.googleapis.com/drive/v3"

# Google occasionally answers Sheets calls with a transient 503/500/429,
# especially right after a period of no traffic (Render free-tier cold
# start coinciding with a Google-side hiccup). These are worth a short
# retry; a bare 401 is handled separately by google_api.call (token
# refresh), and anything else is a real error worth surfacing immediately.
RETRYABLE_STATUSES = (500, 503, 429)
RETRY_DELAYS = (1, 2, 4)  # seconds, exponential backoff


class GoogleAuthError(Exception):
    """Raised on HTTP 401 so the caller can refresh the token and retry."""


class GoogleTransientError(Exception):
    """Raised when Sheets keeps answering 500/503/429 after all retries."""


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _check(resp: aiohttp.ClientResponse, ok=(200, 201)):
    if resp.status == 401:
        raise GoogleAuthError("Google token unauthorized")
    if resp.status in ok:
        return
    text = await resp.text()
    if resp.status in RETRYABLE_STATUSES:
        raise GoogleTransientError(f"Sheets API {resp.status}: {text}")
    raise RuntimeError(f"Sheets API {resp.status}: {text}")


async def _request(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    ok=(200, 201),
    read_json: bool = False,
    **kwargs,
):
    """
    Single place all Sheets/Drive HTTP calls go through. Retries
    transient 500/503/429 responses with backoff (a plain 401 is NOT
    retried here - it propagates immediately as GoogleAuthError so
    google_api.call can refresh the token first, same as before).
    Returns the parsed JSON body if read_json=True, else None. The body is
    read inside the 'async with' block so it's safe even after the
    response/connection is released on return.
    """
    last_err = None
    for delay in (0,) + RETRY_DELAYS:
        if delay:
            await asyncio.sleep(delay)
        async with session.request(method, url, **kwargs) as resp:
            try:
                await _check(resp, ok=ok)
                return await resp.json() if read_json else None
            except GoogleTransientError as e:
                last_err = e
                continue
    raise last_err


# -------------------- layout --------------------

SHEET_TRANSACTIONS = "Транзакции"
SHEET_AUTO = "Авто"
SHEET_MILEAGE = "Пробег"
SHEET_CARS = "Машины"

HEADERS = {
    SHEET_TRANSACTIONS: [
        "ID",
        "Дата и время",
        "Кто",
        "Тип",
        "Категория",
        "Сумма",
        "Источник",
        "Комментарий",
        "Статус",
    ],
    SHEET_AUTO: [
        "ID",
        "Дата",
        "Машина",
        "Тип",
        "Описание",
        "Сумма",
        "Пробег",
        "Кто",
        "Статус",
    ],
    SHEET_MILEAGE: [
        "ID",
        "Дата",
        "Машина",
        "Пробег",
        "Источник",
        "Кто",
    ],
    SHEET_CARS: [
        "ID",
        "Машина",
        "Статус",
        "Дата регистрации",
        "Последнее напоминание",
    ],
}


def new_row_id() -> str:
    """
    Short, good-enough-unique row identifier (not a DB primary key, just
    something stable to find a logical row again after it's been appended).
    Prefixed with a letter on purpose: a pure-hex-digit string has a small
    but real chance (~0.7% per row) of looking like a plain number, and
    Google Sheets can then silently store/return it AS a number (stripping
    leading zeros) instead of text - breaking exact-string lookups later.
    """
    return f"r{uuid.uuid4().hex[:12]}"


# -------------------- create file --------------------


async def create_budget_spreadsheet(session: aiohttp.ClientSession, token: str, title: str) -> str:
    """
    Create a new spreadsheet with all four tabs pre-populated with bold header rows.
    Returns the new spreadsheet's ID.
    """
    body = {
        "properties": {"title": title},
        "sheets": [{"properties": {"title": name}} for name in HEADERS],
    }
    data = await _request(
        session,
        "POST",
        API,
        ok=(200,),
        read_json=True,
        json=body,
        headers=_headers(token),
    )
    spreadsheet_id = data["spreadsheetId"]

    # Map tab title -> sheetId, needed for the formatting batchUpdate below
    sheet_ids = {
        s["properties"]["title"]: s["properties"]["sheetId"] for s in data["sheets"]
    }

    # Write header rows
    value_ranges = [
        {"range": f"{name}!A1", "values": [cols]} for name, cols in HEADERS.items()
    ]
    await _request(
        session,
        "POST",
        f"{API}/{spreadsheet_id}/values:batchUpdate",
        ok=(200,),
        json={"valueInputOption": "RAW", "data": value_ranges},
        headers=_headers(token),
    )

    # Bold header rows + freeze row 1, one request per tab
    requests = []
    for name in HEADERS:
        sid = sheet_ids[name]
        requests.append({
            "repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                "fields": "userEnteredFormat.textFormat.bold",
            }
        })
        requests.append({
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sid,
                    "gridProperties": {"frozenRowCount": 1},
                },
                "fields": "gridProperties.frozenRowCount",
            }
        })

    await _request(
        session,
        "POST",
        f"{API}/{spreadsheet_id}:batchUpdate",
        ok=(200,),
        json={"requests": requests},
        headers=_headers(token),
    )

    return spreadsheet_id


async def create_sheets_from_layout(
    session: aiohttp.ClientSession,
    token: str,
    title: str,
    sheets: dict[str, list[list[str]]],
) -> str:
    """
    Generic 4-tab budget layout. 'sheets' maps tab name -> rows
    (first row treated as the header and bolded); an empty rows list
    still creates the tab, just with no data written.
    """
    body = {
        "properties": {"title": title},
        "sheets": [{"properties": {"title": name}} for name in sheets],
    }
    data = await _request(
        session,
        "POST",
        API,
        ok=(200,),
        read_json=True,
        json=body,
        headers=_headers(token),
    )
    spreadsheet_id = data["spreadsheetId"]
    sheet_ids = {
        s["properties"]["title"]: s["properties"]["sheetId"] for s in data["sheets"]
    }

    value_ranges = [
        {"range": f"{name}!A1", "values": rows}
        for name, rows in sheets.items()
        if rows
    ]
    if value_ranges:
        await _request(
            session,
            "POST",
            f"{API}/{spreadsheet_id}/values:batchUpdate",
            ok=(200,),
            json={"valueInputOption": "RAW", "data": value_ranges},
            headers=_headers(token),
        )

    requests = [
        {
            "repeatCell": {
                "range": {"sheetId": sheet_ids[name], "startRowIndex": 0, "endRowIndex": 1},
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                "fields": "userEnteredFormat.textFormat.bold",
            }
        }
        for name, rows in sheets.items()
        if rows
    ]
    if requests:
        await _request(
            session,
            "POST",
            f"{API}/{spreadsheet_id}:batchUpdate",
            ok=(200,),
            json={"requests": requests},
            headers=_headers(token),
        )

    return spreadsheet_id


# -------------------- append row --------------------


async def append_row(
    session: aiohttp.ClientSession,
    token: str,
    spreadsheet_id: str,
    sheet_name: str,
    values: list,
) -> str:
    """
    Append a row, auto-generating and prepending its ID. Returns the ID.
    """
    row_id = new_row_id()
    row = [row_id] + values
    await _request(
        session,
        "POST",
        f"{API}/{spreadsheet_id}/values/{sheet_name}!A:A:append",
        ok=(200,),
        params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"},
        json={"values": [row]},
        headers=_headers(token),
    )
    return row_id


# -------------------- update row --------------------


async def update_row(
    session: aiohttp.ClientSession,
    token: str,
    spreadsheet_id: str,
    sheet_name: str,
    row_id: str,
    column_name: str,
    new_value: str,
) -> bool:
    """
    Update a single cell in the row with the given ID.
    Returns False if the row_id wasn't found (e.g. user deleted it manually).
    """
    headers = HEADERS[sheet_name]
    if column_name not in headers:
        raise ValueError(f"Unknown column {column_name!r} for sheet {sheet_name!r}")

    id_column = await _request(
        session,
        "GET",
        f"{API}/{spreadsheet_id}/values/{sheet_name}!A2:A",
        ok=(200,),
        read_json=True,
        headers=_headers(token),
    )
    ids = [r[0] if r else "" for r in id_column.get("values", [])]
    if row_id not in ids:
        return False

    row_index = ids.index(row_id) + 2  # +2: header row + 1-based sheet rows
    col_letter = chr(ord("A") + headers.index(column_name))

    await _request(
        session,
        "PUT",
        f"{API}/{spreadsheet_id}/values/{sheet_name}!{col_letter}{row_index}",
        ok=(200,),
        params={"valueInputOption": "USER_ENTERED"},
        json={"values": [[new_value]]},
        headers=_headers(token),
    )
    return True


# -------------------- publish --------------------


async def publish_and_get_url(
    session: aiohttp.ClientSession,
    token: str,
    spreadsheet_id: str,
) -> str:
    """
    Same 'anyone with the link, reader' pattern as PixKeep's drive_utils.py -
    useful e.g. for the one-off export file created when a car is removed.
    """
    await _request(
        session,
        "POST",
        f"{DRIVE_API}/files/{spreadsheet_id}/permissions",
        ok=(200, 201),
        json={"type": "anyone", "role": "reader"},
        headers=_headers(token),
    )
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"


# -------------------- helpers --------------------


def now_iso() -> str:
    """
    Timestamp for the 'Дата и время' / 'Дата' columns -
    local convention used across TwoPockets sheets.
    """
    return datetime.now(timezone.utc).isoformat()


def to_float(value) -> float:
    """
    Sheets values.get can return a cell as either a native JSON number
    or a string depending on how it was entered/formatted - handle both.
    Shared low-level helper; both sheets_transactions.py and cars.py need it.
    """
    if isinstance(value, (int, float)):
        return float(value)
    if not value:
        return 0.0
    cleaned = "".join(ch for ch in str(value).replace(",", ".") if ch.isdigit() or ch in ".-")
    return float(cleaned) if cleaned else 0.0