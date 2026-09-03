"""Resolve a spoken name to a directory person, tolerantly and fast.

Google's `searchDirectoryPeople` is prefix-literal: "Diksha" cannot find
"Deeksha", although they are the same name. This module fetches the whole
directory instead (86 people, one page, one API call), caches it briefly, and
matches locally in tiers:

    1. exact   — the display name, case-folded
    2. subset  — every spoken word appears verbatim in the name
    3. phonetic — every spoken word matches a name word after romanization
                  folding (Diksha == Deeksha == Dixa; see jarvis/names.py)
    4. prefix  — a spoken word of 3+ letters starts a name word (mangled STT)

A single match at tiers 1–3 is CONFIDENT: the tools proceed straight to the
confirmation, which names the person — that gate is what protects against a
wrong recipient, not an interrogation. Tier 4, or several matches, still asks.

Falls back to the server-side prefix search if the listing call fails, so a
directory too large to list (or a permissions hiccup) degrades to the old
behaviour rather than breaking.
"""

from __future__ import annotations

import time

from ..names import name_keys, tokens_match

_CACHE_SECONDS = 600
_SOURCES = ["DIRECTORY_SOURCE_TYPE_DOMAIN_PROFILE",
            "DIRECTORY_SOURCE_TYPE_DOMAIN_CONTACT"]
_READ_MASK = "names,emailAddresses,phoneNumbers"
_MAX_PAGES = 10

# One cache per process: one signed-in user, one directory.
_cache: dict = {"at": 0.0, "people": None}


def display_name(person: dict) -> str:
    return (person.get("names") or [{}])[0].get("displayName", "")


def email(person: dict) -> str:
    return (person.get("emailAddresses") or [{}])[0].get("value", "")


def _fetch_all(people_svc) -> list[dict] | None:
    now = time.monotonic()
    if _cache["people"] is not None and now - _cache["at"] < _CACHE_SECONDS:
        return _cache["people"]
    people, token, pages = [], None, 0
    try:
        while pages < _MAX_PAGES:
            resp = people_svc.people().listDirectoryPeople(
                readMask=_READ_MASK, sources=_SOURCES,
                pageSize=1000, pageToken=token,
            ).execute()
            people.extend(p for p in resp.get("people", []) if p.get("names"))
            token = resp.get("nextPageToken")
            pages += 1
            if not token:
                break
    except Exception:
        return None   # caller falls back to the server-side search
    _cache["people"] = people
    _cache["at"] = now
    return people


def _server_search(people_svc, query: str) -> list[dict]:
    resp = people_svc.people().searchDirectoryPeople(
        query=query, readMask=_READ_MASK, sources=_SOURCES, pageSize=30,
    ).execute()
    return resp.get("people", [])


def resolve(people_svc, spoken_name: str) -> tuple[list[dict], bool]:
    """Match *spoken_name* against the directory.

    Returns (matches, confident). *confident* means a single match found by
    exact or phonetic identity — safe to act on, with the confirmation gate
    still naming the person before anything is sent.
    """
    spoken_name = " ".join(spoken_name.split())
    if not spoken_name:
        return [], False

    everyone = _fetch_all(people_svc)
    if everyone is None:
        # Old behaviour: server prefix search, with the shortened retry.
        matches = _server_search(people_svc, spoken_name)
        if not matches and spoken_name.split():
            first = spoken_name.split()[0]
            for attempt in (first, first[:4]):
                if len(attempt) >= 3:
                    matches = _server_search(people_svc, attempt)
                    if matches:
                        break
        return matches, len(matches) == 1

    folded = spoken_name.casefold()
    spoken_words = [w.casefold() for w in spoken_name.split()]

    # Tier 1: the whole display name.
    exact = [p for p in everyone if display_name(p).casefold() == folded]
    if exact:
        return exact, len(exact) == 1

    # Tier 2: every spoken word appears verbatim in the name.
    def has_all_words(person: dict) -> bool:
        name_words = [w.casefold() for w in display_name(person).split()]
        return all(w in name_words for w in spoken_words)

    verbatim = [p for p in everyone if has_all_words(p)]
    if verbatim:
        return verbatim, len(verbatim) == 1

    # Tier 3: phonetic — Diksha == Deeksha == Dixa.
    phonetic = [p for p in everyone if tokens_match(spoken_name, display_name(p))]
    if phonetic:
        return phonetic, len(phonetic) == 1

    # Tier 4: prefix on phonetic keys, for names STT cut short. Never
    # confident — "Ra" matching Ranjana is a guess, not an identity.
    spoken_keys = [k for k in name_keys(spoken_name) if len(k) >= 3]
    if spoken_keys:
        def key_prefix(person: dict) -> bool:
            person_keys = name_keys(display_name(person))
            return all(any(pk.startswith(sk) or sk.startswith(pk)
                           for pk in person_keys) for sk in spoken_keys)

        close = [p for p in everyone if key_prefix(p)]
        if close:
            return close, False

    # Tier 5: fuzzy on phonetic keys — a dropped or swapped letter inside the
    # name ("Ranjna" for Ranjana) that neither folding nor prefixing covers.
    # Scored, not thresholded alone: at a flat 0.8 floor, "Ranjna" matched
    # Himanshu RANA (0.80) alongside RANJANA (0.92) and manufactured an
    # ambiguity that does not exist. Only candidates within a whisker of the
    # best score survive.
    if spoken_keys:
        from difflib import SequenceMatcher

        def score(person: dict) -> float:
            person_keys = name_keys(display_name(person))
            if not person_keys:
                return 0.0
            per_token = [
                max(SequenceMatcher(None, sk, pk).ratio() for pk in person_keys)
                for sk in spoken_keys
            ]
            return min(per_token)   # every spoken word must find a partner

        scored = [(score(p), p) for p in everyone]
        best = max((s for s, _ in scored), default=0.0)
        if best >= 0.8:
            near = [p for s, p in scored if s >= best - 0.05]
            return near, False

    return [], False


def closest_names(people_svc, spoken_name: str, n: int = 2) -> list[str]:
    """The nearest directory names to a spoken one that matched nothing.

    STT rounds unfamiliar names to familiar ones — "Joshi" arrives as
    "Jyoti" — and scores just below the fuzzy floor, so resolve() correctly
    refuses to guess. But "no directory match" is a dead end that costs the
    user the whole request; "did you mean Mohit Joshi?" costs one word.
    """
    everyone = _fetch_all(people_svc)
    if not everyone:
        return []
    from difflib import SequenceMatcher

    spoken_keys = [k for k in name_keys(spoken_name) if len(k) >= 3]
    if not spoken_keys:
        return []

    def score(person: dict) -> float:
        person_keys = name_keys(display_name(person))
        if not person_keys:
            return 0.0
        return min(
            max(SequenceMatcher(None, sk, pk).ratio() for pk in person_keys)
            for sk in spoken_keys
        )

    # Floor calibrated on real pairs: "Jyoti"/"Joshi" scores 0.67 (a genuine
    # mis-hearing worth offering); "Zoltan"/"Jain" scores 0.60 (noise that
    # must not become a suggestion).
    ranked = sorted(((score(p), display_name(p)) for p in everyone), reverse=True)
    out: list[str] = []
    for s, name in ranked:
        if s < 0.63 or len(out) >= n:
            break
        if name not in out:
            out.append(name)
    return out


def prefer_colleagues(matches: list[dict], own_domain: str) -> tuple[list[dict], bool]:
    """Rank the user's own organisation above external directory contacts.

    "Message Mansi" in a company assistant means the colleague, yet the
    directory also carries admin-published external contacts — the same name
    at a client's domain, even support@ aliases. Announcing all three as
    equal choices reads as a malfunction. If exactly one match is a
    colleague, that one wins; the externals are still reported so the model
    can mention they exist.
    """
    if not own_domain or len(matches) < 2:
        return matches, False
    internal = [p for p in matches
                if email(p).casefold().endswith("@" + own_domain.casefold())]
    if len(internal) == 1:
        return internal, True
    return matches, False
