"""Google OAuth for Drive and Gmail.

Each employee authorizes Jarvis against the company's *internal* Google
Cloud OAuth app (Desktop type) in their own browser — Jarvis never sees or
stores their Google password. Internal-type consent screens skip Google's
verification review entirely.

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

from ..paths import app_data_dir, google_token_file
from ..security import dpapi

_TOKEN_DPAPI_FILE = "google_token.bin"


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
    payload = creds.to_json().encode("utf-8")
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
    token = _load_token()
    if token:
        try:
            creds = Credentials.from_authorized_user_info(token, scopes)
        except ValueError:
            creds = None

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _store_token(creds)
            return creds
        except RefreshError:
            # Revoked, scope change, or admin reset — clear and re-consent.
            clear_token()
            creds = None
        except Exception:
            creds = None

    if not interactive:
        raise GoogleAuthError(
            "Google authorization required. Run `jarvis setup-google` first."
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
    _store_token(creds)
    return creds
