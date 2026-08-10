"""
google_oauth.py
v1.0 - per-user Google OAuth (authorization-code flow, plain REST via aiohttp)

Same OAuth Client ID/Secret as PixKeep (same Google Cloud project) — this is
a different *application* using the same client, not a shared installation.
Each TwoPockets user authorizes their own Google Drive on Google's own
consent page; the bot only ever receives an authorization code -> tokens,
never a password.

Note the state map here carries BOTH the internal Supabase user_id (needed
to write tokens to the users table) and the Telegram tg_id (needed to send
a confirmation message back — main.py's bot instance isn't a module global,
so the web callback route reaches it via request.app["bot"]).
"""
import secrets
from urllib.parse import urlencode

import aiohttp

from config import GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_OAUTH_REDIRECT_URI, GOOGLE_SCOPE

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

# short-lived map: state nonce -> {user_id, tg_id} (survives only until callback)
pending_states: dict[str, dict] = {}


def build_auth_url(user_id: int, tg_id: int) -> str:
    state = secrets.token_urlsafe(24)
    pending_states[state] = {"user_id": user_id, "tg_id": tg_id}
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": GOOGLE_SCOPE,
        "access_type": "offline",   # required to receive a refresh token
        "prompt": "consent",        # force refresh token even on re-auth
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


async def exchange_code(state: str, code: str) -> tuple[dict, str, str]:
    """Return ({user_id, tg_id}, access_token, refresh_token) for a completed
    consent, or raise ValueError if the state is unknown/expired."""
    pending = pending_states.pop(state, None)
    if pending is None:
        raise ValueError("Unknown or expired state")

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": GOOGLE_OAUTH_REDIRECT_URI,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(TOKEN_URL, data=data) as resp:
            payload = await resp.json()
            if resp.status != 200 or "access_token" not in payload:
                raise ValueError(f"Token exchange failed: {payload}")

    if "refresh_token" not in payload:
        raise ValueError(
            "No refresh token returned by Google (usually means the user "
            "already authorized before without revoking access — ask them "
            "to revoke access at myaccount.google.com/permissions and retry)"
        )
    return pending, payload["access_token"], payload["refresh_token"]


async def refresh_access_token(refresh_token: str) -> dict:
    """Return {access_token, refresh_token} using a stored refresh token."""
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(TOKEN_URL, data=data) as resp:
            payload = await resp.json()
            if resp.status != 200 or "access_token" not in payload:
                raise ValueError(f"Token refresh failed: {payload}")
    return {
        "access_token": payload["access_token"],
        # Google normally does NOT return a new refresh_token on refresh —
        # keep reusing the one we already have unless a new one is given.
        "refresh_token": payload.get("refresh_token", refresh_token),
    }


async def get_user_email(access_token: str) -> str:
    async with aiohttp.ClientSession() as session:
        async with session.get(
            USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}
        ) as resp:
            data = await resp.json()
            if resp.status != 200:
                raise ValueError(f"userinfo failed: {data}")
            return data.get("email", "unknown@account")
