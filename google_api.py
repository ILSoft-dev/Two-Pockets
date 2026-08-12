"""
google_api.py
v1.0 - centralized "refresh the Google access token on 401, retry exactly
once" helper.

This was the actual root cause of "траты уходят в тишину" / "ошибка
авторизации при вызове истории": access tokens expire after ~1 hour, and
NONE of sheets_transactions.py / cars.py / car_stats.py / fluid_tracker.py /
reminders.py ever refreshed them — every Sheets API call made after the
first hour post-connection raised sheets_client.GoogleAuthError, uncaught,
crashing the update silently from the user's point of view.

Retrying the whole surrounding function (instead of just the one failed
call) would risk duplicate writes if an earlier call in a multi-step
operation (e.g. save_auto_expense's 2-3 appends) already succeeded before a
later one hit the 401 — so the refresh+retry happens at the level of each
individual Sheets API call, not the function around it (same principle
PixKeep's _upload_all already used for exactly this reason).
"""
import supabase_client as db
from google_oauth import refresh_access_token
from sheets_client import GoogleAuthError


class TokenBox:
    """Mutable holder for one multi-step operation's access token. If a
    mid-operation call hits 401, refresh() updates it in place — subsequent
    calls within the same operation (e.g. the second/third append_row in
    save_auto_expense) automatically reuse the refreshed token instead of
    each independently refreshing again."""

    def __init__(self, account: dict):
        self.account = account
        self.access_token = account["google_access_token"]
        self._refreshed = False

    async def refresh(self) -> str:
        if not self._refreshed:
            new = await refresh_access_token(self.account["google_refresh_token"])
            self.access_token = new["access_token"]
            db.update_google_access_token(
                self.account["id"], new["access_token"], new["refresh_token"]
            )
            self._refreshed = True
        return self.access_token


async def call(box: TokenBox, operation):
    """`operation` is an async callable taking one argument (the access
    token). Tries with box's current token; on GoogleAuthError, refreshes
    (via box, so it's shared across the rest of the same operation) and
    retries exactly once."""
    try:
        return await operation(box.access_token)
    except GoogleAuthError:
        token = await box.refresh()
        return await operation(token)
