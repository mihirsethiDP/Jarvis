from __future__ import annotations

import json

import pytest

from jarvis.integrations import google_auth as ga


class FakeCreds:
    def __init__(self, valid=True, expired=False, refresh_token="rt", granted_scopes=None):
        self.valid = valid
        self.expired = expired
        self.refresh_token = refresh_token
        self.granted_scopes = granted_scopes

    def refresh(self, request):
        self.valid = True

    def to_json(self):
        return json.dumps({"token": "x", "scopes": ["a", "b"]})


class FakeFlow:
    def __init__(self, creds):
        self._creds = creds

    def run_local_server(self, port=0, prompt="consent"):
        return self._creds


@pytest.fixture
def no_dpapi(monkeypatch, tmp_path):
    # Force the plaintext-file fallback path so tests don't touch real DPAPI.
    monkeypatch.setattr(ga.dpapi, "unprotect_from_file", lambda path: None)
    monkeypatch.setattr(ga.dpapi, "protect_to_file", lambda *a, **k: False)
    monkeypatch.setattr(ga, "google_token_file", lambda: tmp_path / "token.json")


def test_scope_expansion_forces_reauth_not_silent_success(no_dpapi, monkeypatch):
    monkeypatch.setattr(
        ga, "_load_token",
        lambda: {"scopes": ["https://www.googleapis.com/auth/drive.file"]},
    )
    monkeypatch.setattr(
        ga.Credentials, "from_authorized_user_info",
        classmethod(lambda cls, info, scopes: FakeCreds(valid=True)),
    )
    with pytest.raises(ga.GoogleAuthError, match="Chat"):
        ga.get_credentials(
            "", [
                "https://www.googleapis.com/auth/drive.file",
                "https://www.googleapis.com/auth/chat.spaces.readonly",
            ],
            interactive=False,
        )


def test_message_names_gmail_specifically_for_send_to_modify_upgrade(no_dpapi, monkeypatch):
    # The exact real-world case this feature targets: an employee authorized
    # under the old gmail.send-only scope list before Chat/Calendar/Directory
    # (and gmail.modify) were added.
    monkeypatch.setattr(
        ga, "_load_token",
        lambda: {"scopes": [
            "https://www.googleapis.com/auth/drive.file",
            "https://www.googleapis.com/auth/drive.readonly",
            "https://www.googleapis.com/auth/gmail.send",
        ]},
    )
    monkeypatch.setattr(
        ga.Credentials, "from_authorized_user_info",
        classmethod(lambda cls, info, scopes: FakeCreds(valid=True)),
    )
    with pytest.raises(ga.GoogleAuthError) as exc_info:
        ga.get_credentials(
            "", [
                "https://www.googleapis.com/auth/drive.file",
                "https://www.googleapis.com/auth/drive.readonly",
                "https://www.googleapis.com/auth/gmail.modify",
            ],
            interactive=False,
        )
    assert "Gmail" in str(exc_info.value)


def test_prefers_granted_scopes_over_requested_scopes_field(no_dpapi, monkeypatch):
    # A token stored after a PARTIAL consent: "scopes" reflects what was
    # requested (both), but "granted_scopes" reflects what the user actually
    # checked on the consent screen (drive only). The real grant must win.
    monkeypatch.setattr(
        ga, "_load_token",
        lambda: {
            "scopes": ["https://www.googleapis.com/auth/drive.file",
                      "https://www.googleapis.com/auth/chat.spaces.readonly"],
            "granted_scopes": ["https://www.googleapis.com/auth/drive.file"],
        },
    )
    monkeypatch.setattr(
        ga.Credentials, "from_authorized_user_info",
        classmethod(lambda cls, info, scopes: FakeCreds(valid=True)),
    )
    with pytest.raises(ga.GoogleAuthError, match="Chat"):
        ga.get_credentials(
            "", [
                "https://www.googleapis.com/auth/drive.file",
                "https://www.googleapis.com/auth/chat.spaces.readonly",
            ],
            interactive=False,
        )


def test_sufficient_scopes_return_valid_creds_without_reauth(no_dpapi, monkeypatch):
    monkeypatch.setattr(
        ga, "_load_token",
        lambda: {"scopes": ["https://www.googleapis.com/auth/drive.file",
                            "https://www.googleapis.com/auth/chat.spaces.readonly"]},
    )
    monkeypatch.setattr(
        ga.Credentials, "from_authorized_user_info",
        classmethod(lambda cls, info, scopes: FakeCreds(valid=True)),
    )
    creds = ga.get_credentials(
        "", ["https://www.googleapis.com/auth/drive.file"], interactive=False,
    )
    assert creds.valid is True


def test_missing_token_gives_generic_message_not_scope_message(no_dpapi, monkeypatch):
    monkeypatch.setattr(ga, "_load_token", lambda: None)
    with pytest.raises(ga.GoogleAuthError, match="^Google authorization required"):
        ga.get_credentials("", ["https://www.googleapis.com/auth/drive.file"], interactive=False)


def test_partial_consent_on_first_run_is_caught(tmp_path, no_dpapi, monkeypatch):
    monkeypatch.setattr(ga, "_load_token", lambda: None)
    creds_file = tmp_path / "client_secret.json"
    creds_file.write_text("{}", encoding="utf-8")
    # The user unchecked a scope on the consent screen: granted_scopes is
    # narrower than what was requested.
    fake_creds = FakeCreds(valid=True, granted_scopes=[
        "https://www.googleapis.com/auth/drive.file",
    ])
    monkeypatch.setattr(
        ga.InstalledAppFlow, "from_client_secrets_file",
        classmethod(lambda cls, path, scopes: FakeFlow(fake_creds)),
    )
    cleared = []
    monkeypatch.setattr(ga, "clear_token", lambda: cleared.append(1))
    with pytest.raises(ga.GoogleAuthError, match="partially granted"):
        ga.get_credentials(
            str(creds_file),
            ["https://www.googleapis.com/auth/drive.file",
             "https://www.googleapis.com/auth/chat.spaces.readonly"],
            interactive=True,
        )
    assert cleared == [1]


def test_full_consent_on_first_run_succeeds(tmp_path, no_dpapi, monkeypatch):
    monkeypatch.setattr(ga, "_load_token", lambda: None)
    creds_file = tmp_path / "client_secret.json"
    creds_file.write_text("{}", encoding="utf-8")
    fake_creds = FakeCreds(valid=True, granted_scopes=[
        "https://www.googleapis.com/auth/drive.file",
        "https://www.googleapis.com/auth/chat.spaces.readonly",
    ])
    monkeypatch.setattr(
        ga.InstalledAppFlow, "from_client_secrets_file",
        classmethod(lambda cls, path, scopes: FakeFlow(fake_creds)),
    )
    monkeypatch.setattr(ga, "_store_token", lambda creds: None)
    creds = ga.get_credentials(
        str(creds_file),
        ["https://www.googleapis.com/auth/drive.file",
         "https://www.googleapis.com/auth/chat.spaces.readonly"],
        interactive=True,
    )
    assert creds is fake_creds


# -- refresh failures ----------------------------------------------------
# Reported from real use: "it's repeatedly asking me to set up Google
# authorization". Access tokens last about an hour, so most sessions begin
# with a refresh; every way that refresh could fail produced the same
# "run setup-google" message, and any RefreshError deleted the token. On a
# slow network that meant re-authorizing session after session.
SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def _expired_token(monkeypatch):
    monkeypatch.setattr(ga, "_load_token", lambda: {
        "token": "x", "refresh_token": "rt", "granted_scopes": SCOPES,
    })


def _creds_that_fail_refresh(monkeypatch, error):
    def from_info(info, scopes):
        creds = FakeCreds(valid=False, expired=True, granted_scopes=SCOPES)

        def boom(request):
            raise error
        creds.refresh = boom
        return creds
    monkeypatch.setattr(ga.Credentials, "from_authorized_user_info",
                        staticmethod(from_info))


def test_network_failure_keeps_the_token_and_says_so(monkeypatch):
    _expired_token(monkeypatch)
    _creds_that_fail_refresh(monkeypatch, OSError("getaddrinfo failed"))
    cleared = []
    monkeypatch.setattr(ga, "clear_token", lambda: cleared.append(1))

    with pytest.raises(ga.GoogleAuthError) as e:
        ga.get_credentials("client.json", SCOPES, interactive=False)

    assert not cleared, "a network blip must never throw the authorization away"
    assert "network problem" in str(e.value)
    assert "still saved" in str(e.value)


def test_revoked_grant_clears_the_token(monkeypatch):
    _expired_token(monkeypatch)
    _creds_that_fail_refresh(
        monkeypatch, ga.RefreshError("invalid_grant: Token has been revoked."))
    cleared = []
    monkeypatch.setattr(ga, "clear_token", lambda: cleared.append(1))

    with pytest.raises(ga.GoogleAuthError) as e:
        ga.get_credentials("client.json", SCOPES, interactive=False)

    assert cleared == [1]
    assert "revoked" in str(e.value)


def test_transient_server_error_does_not_clear_the_token(monkeypatch):
    # Not every RefreshError means the grant is dead — only invalid_grant does.
    _expired_token(monkeypatch)
    _creds_that_fail_refresh(monkeypatch, ga.RefreshError("internal_failure"))
    cleared = []
    monkeypatch.setattr(ga, "clear_token", lambda: cleared.append(1))

    with pytest.raises(ga.GoogleAuthError) as e:
        ga.get_credentials("client.json", SCOPES, interactive=False)

    assert not cleared
    assert "refused to refresh" in str(e.value)
