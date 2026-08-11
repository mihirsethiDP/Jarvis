"""Per-capability permission grants.

Capabilities are coarse, human-meaningful actions ("drive_read",
"email_send", "ai:company-gpt"). The first time a tool needs one, the user
is asked out loud (or in the console) and can grant it once, for the
session, or always. "Always" grants persist in a local JSON file the user
can review and revoke; session grants expire.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from ..io_channel import IOChannel
from ..paths import permissions_file
from .audit import AuditLog

_ALLOW_ALWAYS = {"always", "always allow", "allow always", "hamesha", "हमेशा"}
_ALLOW_SESSION = {"session", "this session", "allow for this session", "for this session",
                  "is session", "इस सेशन"}
_ALLOW_ONCE = {"once", "yes", "allow", "ok", "okay", "sure", "allow once", "yeah", "yep",
               "yup", "go ahead", "please do", "proceed", "carry on", "go for it",
               "fine", "alright", "all right", "of course", "absolutely", "do it",
               # Hindi / Hinglish — kept deliberately narrow: this grants access,
               # so only unambiguous affirmatives belong here. Bare "ji" or
               # "ek baar" are everyday filler a nearby colleague could utter.
               "haan", "haanji", "ji haan", "theek hai", "thik hai", "bilkul",
               "kar do", "kar dijiye", "karo",
               "हाँ", "हां", "जी हाँ", "ठीक है", "बिल्कुल", "कर दो"}
_DENY = {"no", "deny", "don't", "dont", "never", "cancel", "stop",
         "nahi", "nahin", "mat", "mat karo", "rehne do", "नहीं", "मत", "मत करो", "रहने दो"}


# Explicit punctuation strip (incl. Hindi danda) rather than a \w whitelist:
# re's \w drops Unicode combining marks, which would mangle Devanagari (हाँ).
_PUNCTUATION = str.maketrans({c: " " for c in "!\"#$%&()*+,-./:;<=>?@[\\]^`{|}~।॥“”‘’…—–¡¿"})


def normalize_answer(raw: str) -> str:
    """Lowercase and strip punctuation — speech-to-text produces 'Allow once.'"""
    return " ".join(raw.lower().translate(_PUNCTUATION).split())


# Words that flip the phrase after them. "no"/"nahi" are already denials in
# their own right; these are the ones that only negate what follows.
_NEGATORS = {"not", "dont", "cant", "wont", "isnt", "bina", "nahin"}


def _has(answer: str, phrase: str) -> bool:
    """Whole-word containment, ignoring a negated occurrence.

    "yes but not always" must grant once, not forever — matching "always"
    anywhere in the string would quietly turn a deliberately limited grant
    into a permanent one.
    """
    words = answer.split()
    target = phrase.split()
    if not target:
        return False
    for i in range(len(words) - len(target) + 1):
        if words[i:i + len(target)] != target:
            continue
        if i > 0 and words[i - 1] in _NEGATORS:
            continue  # "not always", "don't allow"
        return True
    return False


def classify_answer(answer: str) -> str:
    """Map a spoken answer to 'always' | 'session' | 'once' | 'deny'.

    Nobody answers a spoken question with one bare keyword. The old code
    tested set membership against the *entire* normalized string, so "yes
    please", "allow it", "sure go ahead" and "haan theek hai" all fell
    through to the deny branch — the user granted access out loud and Jarvis
    recorded a refusal, then asked again on the next action.

    Order matters: deny is checked first and wins anywhere in the answer, so
    "no, don't allow that" can never be read as consent. Then the more
    specific grants before the general one, since "always allow" and "allow
    for this session" both contain "allow".
    """
    if not answer:
        return "deny"          # silence is never consent
    if any(_has(answer, p) for p in _DENY):
        return "deny"
    if any(_has(answer, p) for p in _ALLOW_ALWAYS):
        return "always"
    if any(_has(answer, p) for p in _ALLOW_SESSION):
        return "session"
    if any(_has(answer, p) for p in _ALLOW_ONCE):
        return "once"
    return "deny"              # unrecognized fails closed, as before


class PermissionManager:
    def __init__(
        self,
        io: IOChannel,
        audit: AuditLog,
        *,
        store_path: Path | None = None,
        session_grant_minutes: int = 480,
    ):
        self.io = io
        self.audit = audit
        self.store_path = store_path or permissions_file()
        self.session_ttl = session_grant_minutes * 60
        self._session: dict[str, float] = {}  # capability -> expiry epoch
        self._persistent: dict[str, dict] = self._load()

    # -- store ------------------------------------------------------------
    def _load(self) -> dict[str, dict]:
        if self.store_path.exists():
            try:
                data = json.loads(self.store_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def _save(self) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self.store_path.write_text(
            json.dumps(self._persistent, indent=2), encoding="utf-8"
        )

    # -- API ---------------------------------------------------------------
    def granted(self, capability: str) -> bool:
        if self._persistent.get(capability, {}).get("scope") == "always":
            return True
        expiry = self._session.get(capability)
        return expiry is not None and expiry > time.time()

    def denied(self, capability: str) -> bool:
        """True if the user has standing-denied this capability (e.g. during
        the setup wizard). Standing denials are respected without re-asking."""
        return self._persistent.get(capability, {}).get("scope") == "denied"

    def set_grant(self, capability: str, scope: str) -> None:
        """Record an install-time decision: 'always', 'denied', or 'ask'
        (which clears any standing entry so runtime prompting applies)."""
        if scope == "ask":
            self._persistent.pop(capability, None)
        elif scope in ("always", "denied"):
            self._persistent[capability] = {"scope": scope, "granted_at": time.time()}
        else:
            raise ValueError(f"Unknown grant scope: {scope}")
        self._session.pop(capability, None)
        self._save()
        self.audit.record("permission", tool=capability,
                          decision=f"setup_{scope}", ok=scope != "denied")

    def require(self, capability: str, description: str) -> bool:
        """Return True if the capability is granted, asking the user if needed."""
        if self.denied(capability):
            # The employee said no at setup — respect it, don't nag.
            self.audit.record("permission", tool=capability, detail=description,
                              decision="denied_standing", ok=False)
            return False
        if self.granted(capability):
            return True

        try:
            answer = normalize_answer(self.io.ask(
                f"I need permission to {description}. "
                'Say "allow once", "allow for this session", "always allow", or "deny".'
            ))
        except EOFError:
            answer = ""  # input channel closed — fail closed like silence

        verdict = classify_answer(answer)
        if verdict == "always":
            self._persistent[capability] = {"scope": "always", "granted_at": time.time()}
            self._save()
            decision = "granted_always"
            allowed = True
        elif verdict == "session":
            self._session[capability] = time.time() + self.session_ttl
            decision = "granted_session"
            allowed = True
        elif verdict == "once":
            decision = "granted_once"
            allowed = True
        else:
            # Unrecognized answers fail closed — including silence.
            decision = "denied"
            allowed = False

        self.audit.record(
            "permission", tool=capability, detail=description, decision=decision, ok=allowed
        )
        return allowed

    def revoke(self, capability: str) -> bool:
        """Remove a persistent grant (used by `jarvis permissions revoke`)."""
        removed = self._persistent.pop(capability, None) is not None
        self._session.pop(capability, None)
        if removed:
            self._save()
        self.audit.record("permission", tool=capability, decision="revoked", ok=True)
        return removed

    def list_grants(self) -> dict[str, dict]:
        return dict(self._persistent)
