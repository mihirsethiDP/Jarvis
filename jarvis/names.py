"""Phonetic matching for romanized Indian names.

There is no single correct Latin spelling of most Indian names: दीक्षा is
written Deeksha, Diksha, or Dixa; दीपक is Deepak or Dipak; वर्मा is Verma or
Varma. Speech recognition picks one spelling, the company directory holds
another, and a literal comparison calls them different people.

Measured failure this fixes: the user asked for "Deeksha", STT wrote
"Diksha", Google's prefix-literal directory search found nothing, and a
30-second request became a 2.5-minute interrogation that ended with the user
quitting before the message was sent.

`phonetic_key` folds the common romanization variants down to one canonical
form, so Deeksha/Diksha/Dixa all become "diksa". It is deliberately built for
this one job — matching a spoken Indian name against a company directory —
and not a general soundex.
"""

from __future__ import annotations

import re

# Ordered: multi-character folds must run before their single-character parts.
_FOLDS = [
    ("chh", "c"), ("ch", "c"), ("sh", "s"), ("ph", "f"),
    ("kh", "k"), ("gh", "g"), ("th", "t"), ("dh", "d"), ("bh", "b"),
    ("aa", "a"), ("ee", "i"), ("ii", "i"), ("oo", "u"), ("uu", "u"),
    ("x", "ks"),   # Dixit / Dikshit, Dixa / Diksha
    ("z", "j"),    # Zoya / Joya
    ("w", "v"),    # Warma / Varma
    ("q", "k"),
    ("c", "k"),    # after ch->c: Chaturvedi -> caturvedi -> katurvedi
    ("e", "a"),    # Verma / Varma, Mehta / Mahta — AFTER ee->i, which wins
]

_NON_ALPHA = re.compile(r"[^a-z]+")


def phonetic_key(token: str) -> str:
    """One canonical form per name, across common romanizations."""
    s = _NON_ALPHA.sub("", token.lower())
    for src, dst in _FOLDS:
        s = s.replace(src, dst)
    # Collapse repeats left over after folding (Villa/Vila, Anna/Ana).
    return re.sub(r"(.)\1+", r"\1", s)


def name_keys(full_name: str) -> list[str]:
    """Phonetic key per word of a name, empty tokens dropped."""
    return [k for k in (phonetic_key(w) for w in full_name.split()) if k]


def tokens_match(query: str, candidate: str) -> bool:
    """True when every word of *query* phonetically matches a word of
    *candidate* — order-free, so "Chaturvedi Deeksha" still hits."""
    q = name_keys(query)
    c = name_keys(candidate)
    if not q or not c:
        return False
    remaining = list(c)
    for qk in q:
        for i, ck in enumerate(remaining):
            if qk == ck:
                del remaining[i]
                break
        else:
            return False
    return True
