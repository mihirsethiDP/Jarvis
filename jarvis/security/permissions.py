"""Per-capability permission grants.

Capabilities are coarse, human-meaningful actions ("drive_read",
"email_send", "ai:company-gpt"). The first time a tool needs one, the user
is asked out loud (or in the console) and can grant it once, for the
session, or always. "Always" grants persist in a local JSON file the user
can review and revoke; session grants expire.
"""

from __future__ import annotations

import json
import os
import sys
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


def _is_unclear_grant(answer: str) -> bool:
    """True when the answer was neither a recognisable grant nor a refusal."""
    if not answer:
        return True
    if any(_has(answer, p) for p in _DENY):
        return False                      # an explicit no is not second-guessed
    return classify_answer(answer) == "deny"


class PermissionManager:
    def __init__(
        self,
        io: IOChannel,
        audit: AuditLog,
        *,
        store_path: Path | None = None,
        session_grant_minutes: int = 480,
        on_status=None,
    ):
        self.io = io
        self.audit = audit
        self.on_status = on_status or (lambda *_a: None)
        self.store_path = store_path or permissions_file()
        self.session_ttl = session_grant_minutes * 60
        self._session: dict[str, float] = {}  # capability -> expiry epoch
        self._persistent: dict[str, dict] = self._load()

    # -- store ------------------------------------------------------------
    def _load(self) -> dict[str, dict]:
        if not self.store_path.exists():
            return {}
        try:
            data = json.loads(self.store_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
            raise ValueError("permissions file is not an object")
        except (json.JSONDecodeError, OSError, ValueError) as e:
            # Starting empty was silent, and the next _save() overwrote the
            # file — so a half-written read turned into the permanent loss of
            # every decision made in the setup wizard. Keep the evidence and
            # say so out loud.
            backup = self.store_path.with_suffix(".corrupt.json")
            try:
                backup.write_bytes(self.store_path.read_bytes())
            except OSError:
                backup = None
            print(
                f"Warning: could not read {self.store_path.name} ({e}). Your saved "
                "permission decisions are not being applied this session"
                + (f"; the file was preserved as {backup.name}." if backup else ".")
                + " Re-run the setup wizard to restore them.",
                file=sys.stderr,
            )
            return {}

    def _save(self) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic replace: a truncate-then-write leaves a window where a
        # concurrent read sees an empty file and treats every capability as
        # ungranted.
        tmp = self.store_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._persistent, indent=2), encoding="utf-8")
        os.replace(tmp, self.store_path)

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
            self.on_status("listening", f"waiting for permission — {description}")
            answer = normalize_answer(self.io.ask(
                f"I need permission to {description}. "
                'Say "allow once", "allow for this session", "always allow", or "deny".'
            ))
            # One mis-transcribed answer used to be indistinguishable from a
            # refusal, and for a once-per-session question like memory recall
            # that silently switched the feature off for the whole session.
            # An unintelligible answer gets a second try; a clear "deny" does
            # not, and two unclear answers still fail closed.
            if _is_unclear_grant(answer):
                answer = normalize_answer(self.io.ask(
                    "Sorry, I didn't catch that. Say allow or deny."
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
