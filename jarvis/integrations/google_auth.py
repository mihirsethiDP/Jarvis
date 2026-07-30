"""Google OAuth for Drive, Gmail, Chat, Calendar, and Directory lookup.

Each employee authorizes Jarvis against the company's *internal* Google
Cloud OAuth app (Desktop type) in their own browser — Jarvis never sees or
stores their Google password. Internal-type consent screens skip Google's
verification review entirely. One consent flow requests every scope Jarvis
is configured to use, across every Google app it services.

Token storage: OAuth token JSON is too large for the Windows keyring's
2560-byte blob limit, so it is kept in a DPAPI-encrypted file under
%APPDATA%\\Jarvis (decryptable only by this Windows user on this machine).
If DPAPI is unavailable the token falls back to a plain profile-local file
and a warning is printed.
"""

from __future__ import annotations

import json
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from ..paths import app_data_dir, cli_hint, google_token_file
from ..security import dpapi

_TOKEN_DPAPI_FILE = "google_token.bin"

# Friendly names for the missing-scopes error message, keyed by a substring
# of the scope URL (checked in order — most specific first).
_SCOPE_PRODUCT_NAMES = [
    ("drive", "Drive"), ("gmail", "Gmail"), ("chat.", "Chat"),
    ("calendar", "Calendar"), ("directory", "Directory"), ("contacts", "Contacts"),
]


def _product_names(scopes: set[str]) -> list[str]:
    names = set()
    for scope in scopes:
        for needle, label in _SCOPE_PRODUCT_NAMES:
            if needle in scope:
                names.add(label)
                break
    return sorted(names)


class GoogleAuthError(RuntimeError):
    pass


def _dpapi_path() -> Path:
    return app_data_dir() / _TOKEN_DPAPI_FILE


def _load_token() -> dict | None:
    data = dpapi.unprotect_from_file(_dpapi_path())
    if data is None:
        fallback = google_token_file()
        if fallback.exists():
            try:
                data = fallback.read_bytes()
            except OSError:
                return None
        else:
            return None
    try:
        return json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _store_token(creds: Credentials) -> None:
    # Credentials.to_json() serializes `scopes` (what was REQUESTED when the
    # flow was built), not `granted_scopes` (what the token endpoint actually
    # returned) — Google supports partial/granular consent, so these can
    # differ. Persist the real grant separately so a later scope-sufficiency
    # check isn't fooled by a token that only ever recorded the request.
    data = json.loads(creds.to_json())
    if getattr(creds, "granted_scopes", None):
        data["granted_scopes"] = list(creds.granted_scopes)
    payload = json.dumps(data).encode("utf-8")
    if not dpapi.protect_to_file(_dpapi_path(), payload, "jarvis-google-token"):
        print(
            "Warning: DPAPI unavailable — storing the Google token unencrypted in "
            f"{google_token_file()}. Install pywin32 for encrypted storage."
        )
        google_token_file().write_bytes(payload)


def clear_token() -> None:
    for path in (_dpapi_path(), google_token_file()):
        if path.exists():
            path.unlink()


def get_credentials(
    credentials_file: str, scopes: list[str], *, interactive: bool = True
) -> Credentials:
    """Return valid user credentials, refreshing or running the consent flow."""
    creds: Credentials | None = None
    missing_products: list[str] = []
    token = _load_token()
    if token:
        try:
            creds = Credentials.from_authorized_user_info(token, scopes)
        except ValueError:
            creds = None
        else:
            # A refresh token is bound to the scopes granted at consent time —
            # refreshing it can never add scopes. If config now requires more
            # than was ever consented to (e.g. Chat/Calendar/Directory added
            # after an earlier Drive/Gmail-only setup), silently "succeeding"
            # here would just 403 at call time on the new APIs. Force a fresh
            # consent instead of a confusing runtime failure. Prefer the real
            # granted_scopes (falls back to the older "scopes" key for tokens
            # written before this check existed).
            granted = set(token.get("granted_scopes") or token.get("scopes") or [])
            missing = set(scopes) - granted
            if missing:
                missing_products = _product_names(missing)
                creds = None

    if creds and creds.valid:
        return creds

    refresh_problem = ""
    if creds and creds.expired and creds.refresh_token:
        # Access tokens last about an hour, so most sessions begin with a
        # refresh. On a slow network that call can fail for reasons that have
        # nothing to do with consent — and treating those as "not authorized"
        # is what makes Jarvis demand re-authorization session after session.
        for attempt in range(2):
            try:
                creds.refresh(Request())
                _store_token(creds)
                return creds
            except RefreshError as e:
                # Only a grant Google has actually rejected justifies throwing
                # the token away. `invalid_grant` means revoked, expired, or
                # password-reset; anything else here may still be transient.
                if "invalid_grant" in str(e).lower():
                    clear_token()
                    refresh_problem = "revoked"
                else:
                    refresh_problem = f"rejected: {e}"
                creds = None
                break
            except Exception as e:
                # Transport/DNS/proxy failure. Keep the token — it is very
                # probably still good once the network comes back.
                refresh_problem = f"unreachable: {type(e).__name__}"
                creds = None
                if attempt == 0:
                    continue
        # fall through to the messages below

    if not interactive:
        if missing_products:
            raise GoogleAuthError(
                "Jarvis needs additional Google permissions you haven't granted "
                f"yet ({', '.join(missing_products)} access). Run "
                f"{cli_hint('setup-google')} to re-authorize."
            )
        if refresh_problem == "revoked":
            raise GoogleAuthError(
                "Google has revoked Jarvis's access (password change, admin reset, "
                f"or you removed it). Run {cli_hint('setup-google')} to sign in again."
            )
        if refresh_problem.startswith("unreachable"):
            raise GoogleAuthError(
                "Couldn't reach Google to refresh your sign-in — this looks like a "
                "network problem, not a permissions one. Your authorization is "
                "still saved; check the connection or VPN and try again. "
                f"({refresh_problem})"
            )
        if refresh_problem:
            raise GoogleAuthError(
                f"Google refused to refresh your sign-in ({refresh_problem}). "
                f"If this repeats, run {cli_hint('setup-google')}."
            )
        if _load_token() is None:
            raise GoogleAuthError(
                f"Google authorization required. Run {cli_hint('setup-google')} first."
            )
        raise GoogleAuthError(
            "Your saved Google sign-in could not be read — it may be from another "
            f"Windows user or a different machine. Run {cli_hint('setup-google')}."
        )

    if not credentials_file or not Path(credentials_file).expanduser().exists():
        raise GoogleAuthError(
            "No Google OAuth client file configured. Set google.credentials_file in "
            "config.yaml to your company's OAuth client secrets JSON (Desktop type)."
        )

    flow = InstalledAppFlow.from_client_secrets_file(
        str(Path(credentials_file).expanduser()), scopes
    )
    creds = flow.run_local_server(port=0, prompt="consent")
    granted = set(getattr(creds, "granted_scopes", None) or scopes)
    if not set(scopes).issubset(granted):
        # Google supports granular consent — the user may have unchecked some
        # boxes on the consent screen. Catch a partial grant on the very
        # first run rather than only ever detecting it on a later reload.
        clear_token()
        raise GoogleAuthError(
            "Google authorization was only partially granted — please check every "
            f"box on the consent screen and run {cli_hint('setup-google')} again."
        )
    _store_token(creds)
    return creds
