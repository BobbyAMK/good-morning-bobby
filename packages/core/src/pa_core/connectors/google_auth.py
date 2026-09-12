"""Google OAuth desktop flow, with tokens in the macOS Keychain.

THE operational decision is not about storage, it is about publishing status: set the
OAuth app to "In production" (unverified is fine). In "Testing" Google issues a refresh
token that expires after 7 DAYS, so a scheduled agent would demand re-auth every Monday
and be abandoned within a month. Publishing unverified costs one "Google hasn't verified
this app" screen and a lifetime cap of 100 users -- you have one.

You do NOT need verification or the annual third-party security assessment: Google
lists personal use and development as explicit exceptions for restricted scopes.

Three ways the token still dies:
  1. Testing status -> 7 days.         (fixed by publishing)
  2. Unused for 6 months -> dead.      (the daily doctor call keeps it warm)
  3. >100 refresh tokens per client id -> the oldest is silently killed.
     So do not re-run the consent flow casually.
"""

from __future__ import annotations

import json
from typing import Final

import keyring
import structlog
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from ..errors import GoogleAuthExpired, GoogleAuthMissing
from ..secrets import GOOGLE_SERVICE

log = structlog.get_logger("pa.google")

_TOKEN_KEY: Final = "oauth_token"
_CLIENT_KEY: Final = "oauth_client"  # the downloaded client_secret JSON, also in Keychain

# Start MINIMAL. Project 1 needs reads only; project 3 upgrades gmail.readonly ->
# gmail.modify. Each widening is a re-consent, and a narrower scope is a smaller blast
# radius and one fewer restricted-scope conversation with Google.
SCOPES_P1: Final = [
    "https://www.googleapis.com/auth/gmail.readonly",  # restricted
    "https://www.googleapis.com/auth/calendar.readonly",  # sensitive
]

# Project 3. gmail.modify is the ONLY single scope covering read + label + drafts:
# gmail.readonly + gmail.compose cannot apply labels via messages.modify.
SCOPES_P3: Final = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar.readonly",
]


def store_client_config(client_json: str) -> None:
    json.loads(client_json)  # fail fast on malformed paste
    keyring.set_password(GOOGLE_SERVICE, _CLIENT_KEY, client_json)


def has_client_config() -> bool:
    return keyring.get_password(GOOGLE_SERVICE, _CLIENT_KEY) is not None


def _load(scopes: list[str]) -> Credentials | None:
    raw = keyring.get_password(GOOGLE_SERVICE, _TOKEN_KEY)
    if not raw:
        return None
    return Credentials.from_authorized_user_info(json.loads(raw), scopes)


def _save(creds: Credentials) -> None:
    keyring.set_password(GOOGLE_SERVICE, _TOKEN_KEY, creds.to_json())


def granted_scopes() -> set[str]:
    """Used by `doctor` to detect silent scope drift after a Google policy change."""
    raw = keyring.get_password(GOOGLE_SERVICE, _TOKEN_KEY)
    return set(json.loads(raw).get("scopes", [])) if raw else set()


def get_credentials(*, scopes: list[str] | None = None, interactive: bool = False) -> Credentials:
    """interactive=False is MANDATORY for scheduled runs.

    A 6am job must never try to open a browser and hang forever -- it fails loudly and
    the watchdog alerts instead.
    """
    scopes = scopes or SCOPES_P1
    creds = _load(scopes)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save(creds)  # a refresh can rotate the refresh token
            return creds
        except RefreshError as e:
            if not interactive:
                raise GoogleAuthExpired(
                    "Google refresh token rejected (revoked, password change, 7-day "
                    "Testing expiry, 6-month idle, or pushed out by the 100-token cap). "
                    "Run: pa-core-admin google-login"
                ) from e
            log.warning("google.refresh_failed_reauthorizing", error=str(e)[:200])

    if not interactive:
        raise GoogleAuthMissing(
            "no Google credentials in Keychain. Run: pa-core-admin google-login"
        )

    raw = keyring.get_password(GOOGLE_SERVICE, _CLIENT_KEY)
    if not raw:
        raise GoogleAuthMissing(
            "no OAuth client config. Download the Desktop client JSON from Google "
            "Cloud Console, then: pa-core-admin google-client --file <path>"
        )
    flow = InstalledAppFlow.from_client_config(json.loads(raw), scopes)
    creds = flow.run_local_server(
        port=0,  # ephemeral; needs an http://localhost redirect URI
        access_type="offline",  # required to GET a refresh token at all
        prompt="consent",  # required to get a NEW one on re-auth
        open_browser=True,
        authorization_prompt_message="Opening browser to authorize Google access...",
    )
    _save(creds)
    return creds


def build_service(
    api: str, version: str, *, scopes: list[str] | None = None, interactive: bool = False
):
    """cache_discovery=False, or every run logs a spurious
    `file_cache is only supported with oauth2client<4.0.0` warning."""
    from googleapiclient.discovery import build

    return build(
        api,
        version,
        credentials=get_credentials(scopes=scopes, interactive=interactive),
        cache_discovery=False,
    )


def health(scopes: list[str] | None = None) -> tuple[bool, str]:
    """One cheap real call per API. Also keeps the token warm against the 6-month
    idle expiry -- that is a deliberate side effect, not a coincidence."""
    scopes = scopes or SCOPES_P1
    try:
        gmail = build_service("gmail", "v1", scopes=scopes)
        prof = gmail.users().getProfile(userId="me").execute()  # 1 quota unit
        cal = build_service("calendar", "v3", scopes=scopes)
        cal.calendarList().list(maxResults=1).execute()
        return True, f"google ok ({prof.get('emailAddress')})"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"[:300]
