"""Explicit confirmation before side effects.

Permission answers *may this class of action happen at all*; confirmation
answers *should this specific action happen right now*. Anything that sends
data off the machine or modifies data (email, uploads, file writes) reads a
summary back to the user and requires an explicit yes — even if the
capability was granted "always".

People misspeak, and they answer confirmations like humans, not like forms:
"yes please", "haan bhej do", "no wait — make it 4pm". So:

- Any deny-word anywhere in the answer cancels (deny always wins — "yes,
  actually no" is a no).
- Otherwise, an answer that IS or STARTS WITH an unambiguous yes confirms.
- Anything else fails closed, but the user's words are preserved on the
  result so the model can treat them as a correction and propose an updated
  action — instead of the correction being silently thrown away.
"""

from __future__ import annotations

from ..io_channel import IOChannel
from .audit import AuditLog
from .permissions import normalize_answer

_YES = {"yes", "confirm", "confirmed", "do it", "go ahead", "send it", "yep", "yeah", "ok", "okay",
        # Every one of these was observed to be rejected by the old matcher,
        # which turned an out-loud "sure, go ahead" into a cancellation and
        # then asked again. Still deliberately unambiguous affirmatives only —
        # nothing hedged ("maybe", "I think so") belongs in a consent gate.
        "sure", "sure thing", "please do", "proceed", "carry on", "go for it",
        "correct", "absolutely", "alright", "all right", "fine", "yup",
        "affirmative", "sounds good", "that's right", "thats right", "send",
        # Hindi / Hinglish — kept deliberately narrow: this is the consent gate,
        # so only unambiguous affirmatives belong here.
        "haan", "haanji", "ji haan", "theek hai", "thik hai", "kar do", "bhej do",
        "bilkul", "han", "haa", "kar dijiye", "bhej dijiye", "theek", "thik",
        "हाँ", "हां", "जी हाँ", "ठीक है", "कर दो", "भेज दो", "बिल्कुल"}

# Speech starts with noise: "um, yes", "okay so send it", "actually yes".
# Stripped before matching so the affirmative underneath is still found.
_LEADING_FILLERS = ("um", "umm", "uh", "er", "hmm", "so", "well", "actually",
                    "please", "just", "haan to", "to")

# Affirmative phrases that happen to contain a deny word. Neutralised before
# the deny scan, so "no problem" stops cancelling the action it agrees to.
# Only settled idioms belong here — anything genuinely ambiguous must keep
# failing closed.
_AFFIRMATIVE_IDIOMS = ("no problem", "no problems", "no issue", "no issues",
                       "no worries", "no doubt", "koi baat nahi", "koi dikkat nahi")

# Single deny-words: if any of these appears anywhere in the answer, the
# action is cancelled regardless of what else was said.
_NO_WORDS = {"no", "nahi", "nahin", "mat", "cancel", "stop", "wait", "dont", "don't",
             "never", "ruko", "नहीं", "मत", "रुको", "galat", "गलत"}


def _strip_idioms(normalized: str) -> str:
    for idiom in _AFFIRMATIVE_IDIOMS:
        normalized = normalized.replace(idiom, " ")
    return " ".join(normalized.split())


def _is_yes(normalized: str) -> bool:
    if not normalized:
        return False
    had_idiom = _strip_idioms(normalized) != normalized
    normalized = _strip_idioms(normalized)
    if had_idiom and not normalized:
        return True          # the whole answer was "no problem"
    words = normalized.split()
    if any(w in _NO_WORDS for w in words):
        return False  # deny wins, always
    if normalized in _YES:
        return True
    # Drop leading filler so "um, yes" and "okay so send it" still read as
    # the affirmative they are.
    while words and words[0] in _LEADING_FILLERS:
        words = words[1:]
    if not words:
        return False
    if " ".join(words) in _YES:
        return True
    # Natural speech pads affirmatives: "yes please", "haan kar do".
    return words[0] in _YES or " ".join(words[:2]) in _YES


class ConfirmResult:
    """Truthy iff confirmed. When declined with more than a plain no, the
    user's words are kept in .correction so tools can hand them back to the
    model ("no, send it to Priya instead" should change the plan, not die)."""

    def __init__(self, confirmed: bool, answer: str = "", refusal_reason: str = ""):
        self.confirmed = confirmed
        self.answer = answer
        # Set when something other than the user blocked this — the safety
        # limiter. Without it the model was told "the user did not confirm"
        # and blamed the employee for a ceiling they never chose.
        self.refusal_reason = refusal_reason
        normalized = normalize_answer(answer)
        plain_refusal = (not normalized) or all(
            w in _NO_WORDS for w in normalized.split()
        )
        self.correction = "" if (confirmed or plain_refusal) else answer

    def __bool__(self) -> bool:
        return self.confirmed


class Confirmer:
    def __init__(self, io: IOChannel, audit: AuditLog, *, enabled: bool = True,
                 limiter=None):
        self.io = io
        self.audit = audit
        self.enabled = enabled
        # Every side effect funnels through confirm(), so the blast-radius
        # cap lives here and covers all of them at once.
        self.limiter = limiter

    def confirm(self, action: str, summary: str, *, audit_detail: str | None = None) -> ConfirmResult:
        """Read the action back to the user; only an explicit yes proceeds.

        *summary* is spoken to the user and should be concrete (it may quote
        content); *audit_detail* is what lands in the audit log — pass a
        content-free variant when the summary contains message bodies.
        """
        detail = audit_detail if audit_detail is not None else summary

        # Refuse before asking: if this action is over its safety ceiling,
        # a "yes" shouldn't be able to authorise it either.
        if self.limiter is not None:
            reason = self.limiter.check(action)
            if reason is not None:
                self.audit.record("confirmation", tool=action, detail=detail,
                                  decision="rate_limited", ok=False)
                self.io.say(
                    f"I've stopped short of that — {reason}. If this is "
                    "genuinely needed, it has to be done manually or the limit "
                    "raised deliberately."
                )
                return ConfirmResult(False, refusal_reason=reason)

        if not self.enabled:
            self.audit.record("confirmation", tool=action, detail=detail,
                              decision="skipped_disabled", ok=True)
            return ConfirmResult(True)

        try:
            raw = self.io.ask(
                f"Please confirm — {summary} Say yes to proceed, or no to cancel."
            )
        except EOFError:
            raw = ""  # input channel closed — treat as cancelled
        confirmed = _is_yes(normalize_answer(raw))
        self.audit.record(
            "confirmation",
            tool=action,
            detail=detail,
            decision="confirmed" if confirmed else "cancelled",
            ok=confirmed,
        )
        if confirmed and self.limiter is not None:
            # Recorded here, at confirmation, rather than handed to each tool
            # to record on success. Deferring it would mean a tool that forgot
            # to call back never counted the action at all, and the ceiling
            # would quietly stop being enforced. Over-counting a failed send
            # is the safe direction to be wrong in.
            self.limiter.record(action)
        return ConfirmResult(confirmed, raw)
