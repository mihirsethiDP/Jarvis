"""Intent fast-path: answer the most common commands without the LLM.

This is the car-assistant trick, borrowed deliberately and kept on a short
leash. Siri and car assistants feel instant because their frequent commands
never touch a reasoning model — a matcher maps the sentence straight to an
action. For DP Assistant's highest-frequency requests, the Claude round trip
(4–25s of a turn) is replaced by a regex match and direct tool calls (~1s
plus the tools' own confirmation gates, which apply unchanged).

Two hard rules keep this safe:

1. CONSERVATIVE — a pattern must match the whole utterance shape, with the
   extracted name bounded and sane. Anything else returns None and the full
   model handles it exactly as before. A wrong fast-path answer costs trust;
   a missed fast-path costs seconds.
2. NO NEW AUTHORITY — the fast path calls the same gated tools the model
   would. Permissions, confirmations, vouching, rate limits, and the audit
   log all apply identically.

There is also a second, softer tier: `wants_fast_model` flags read-only
lookup questions ("kitne unread emails", "what's on my calendar") that still
need the model but not the big one — the app routes those to the fast model
from config.
"""

from __future__ import annotations

import re
from datetime import datetime

_DEVANAGARI = re.compile(r"[ऀ-ॿ]")

# What may appear as a person's name in a fast-matched command: one to three
# words of letters (Latin or Devanagari), no digits, no verbs we know.
_NAME_OK = re.compile(r"^[A-Za-zऀ-ॿ]+(?:\s+[A-Za-zऀ-ॿ]+){0,2}$")
_NAME_STOPWORDS = {
    "message", "msg", "chat", "google", "email", "mail", "group", "team",
    "everyone", "sab", "sabko", "मैसेज", "मेसेज", "ईमेल", "ग्रुप", "टीम",
}

_MSG_WORD = r"(?:message|msg|मैसेज|मेसेज|मैसज)"
_SEND_VERB = (r"(?:send\s+kardo|send\s+kar\s+do|send\s+karo|send|bhejo|"
              r"bhej\s+do|bhej\s+dijiye|bhejna\s+hai|kardo|kar\s+do|karo|"
              r"भेजो|भेज\s+दो|भेज\s+दीजिए|भेजना\s+है|करो|कर\s+दो|bejo|bej\s+do)")
_TELL_VERB = r"(?:bata\s+do|bata\s+dena|bata\s+dijiye|बता\s+दो|बता\s+देना|बता\s+दीजिए)"
_KI = r"(?:ki|kii|that|saying|कि|की|के)"
_LEAD = r"(?:(?:yaar|yar|arey|hey|please|यार|अरे)[,\s]+)*(?:google\s+chat\s+(?:pe|par|पे|पर)\s+)?"

# Order matters: try the most specific shapes first.
_CHAT_SEND_PATTERNS = [
    # "message send kardo Deeksha ko ki ..." / "message bhejna hai Mansi ko ki ..."
    re.compile(
        _LEAD + _MSG_WORD + r"\s+" + _SEND_VERB +
        r"\s+(?P<person>.+?)\s+(?:ko|को)\s+" + _KI + r"\s+(?P<body>.+)$",
        re.IGNORECASE),
    # "Deeksha ko message bhejo ki ..." / "दीक्षा को मैसेज भेज दो कि ..."
    re.compile(
        _LEAD + r"(?P<person>.+?)\s+(?:ko|को)\s+" + _MSG_WORD + r"\s+" +
        _SEND_VERB + r"\s+" + _KI + r"\s+(?P<body>.+)$",
        re.IGNORECASE),
    # "send a message to Deeksha that ..."
    re.compile(
        _LEAD + r"send\s+(?:a\s+)?" + _MSG_WORD +
        r"\s+to\s+(?P<person>.+?)\s+(?:that|saying)\s+(?P<body>.+)$",
        re.IGNORECASE),
    # "Deeksha ko bata do ki ..." — telling a colleague something, in an
    # office wired to Chat, is sending them a message; the confirmation and
    # announcement still name the recipient before anything goes out.
    re.compile(
        _LEAD + r"(?P<person>.+?)\s+(?:ko|को)\s+" + _TELL_VERB +
        r"\s+" + _KI + r"\s+(?P<body>.+)$",
        re.IGNORECASE),
]

# Read-only quick questions, answered by jarvis/tools/quick.py without the
# model. Anchored: "kitne unread emails hai" matches; "check my unread mail
# and reply to the urgent one" must not.
_UNREAD_PATTERNS = re.compile(
    r"^(?:(?:how many|kitne|कितने|kitni|कितनी)\s+(?:unread|naye|नए|new)\s+"
    r"(?:emails?|mails?|ईमेल|मेल)(?:\s+(?:do i have|hain|hai|हैं|है|are there|"
    r"aaye hain|आए हैं))?(?:\s+(?:from\s+)?(?:today|aaj|आज))?"
    r"|(?:unread|naye|नए)\s+(?:emails?|mails?|ईमेल|मेल)\s+"
    r"(?:kitne|kitni|कितने|कितनी)(?:\s+(?:hain|hai|हैं|है))?"
    r"|check\s+(?:my\s+)?unread\s+(?:emails?|mails?))\s*[?.!]*$",
    re.IGNORECASE)

_LATEST_EMAIL_PATTERNS = re.compile(
    r"^(?:read\s+(?:my\s+|me\s+)?(?:the\s+)?(?:latest|last|newest|recent)\s+"
    r"(?:email|mail)|(?:latest|last)\s+(?:email|mail)\s+(?:padho|padhkar sunao|"
    r"sunao|batao|पढ़ो|सुनाओ|बताओ)|(?:sabse\s+)?(?:nayi|नई|naya|नया)\s+"
    r"(?:email|mail|ईमेल|मेल)\s+(?:padho|sunao|batao|पढ़ो|सुनाओ|बताओ)"
    r"|what(?:'s| is) my (?:latest|last) (?:email|mail))\s*[?.!]*$",
    re.IGNORECASE)

_CALENDAR_PATTERNS = re.compile(
    r"^(?:what(?:'s| is| do i have)?\s+(?:on\s+)?(?:my\s+)?"
    r"(?:calendar|schedule)(?:\s+(?:for|look like))?\s+"
    r"(?P<day_en>today|tomorrow)"
    r"|(?P<day_hi>aaj|आज|kal|कल)\s+(?:ki|की|ka|का)?\s*"
    r"(?:meetings?|मीटिंग(?:्स|ें)?|schedule|शेड्यूल|calendar|कैलेंडर)\s*"
    r"(?:kya|क्या)?\s*(?:hai|hain|है|हैं|batao|बताओ|dikhao|दिखाओ)?"
    r"|(?:meetings?|मीटिंग)\s+(?:kya|क्या)\s+(?:hai|hain|है|हैं)\s+"
    r"(?P<day_hi2>aaj|आज|kal|कल)"
    r"|what meetings do i have (?P<day_en2>today|tomorrow))\s*[?.!]*$",
    re.IGNORECASE)


def _day_offset(m: re.Match) -> int:
    day = next((m.group(g) for g in ("day_en", "day_hi", "day_hi2", "day_en2")
                if m.groupdict().get(g)), "today").lower()
    return 1 if day in ("tomorrow", "kal", "कल") else 0

_TIME_PATTERNS = re.compile(
    r"^(?:(?:what(?:'s| is)?\s+the\s+)?time(?:\s+(?:kya|क्या)\s+(?:hai|है))?"
    r"|what time is it|kitne baje(?:\s+(?:hai|hain|है|हैं))?"
    r"|कितने बजे(?:\s+(?:हैं|है))?|time batao|टाइम बताओ)\s*[?.!]*$",
    re.IGNORECASE)

_DATE_PATTERNS = re.compile(
    r"^(?:what(?:'s| is)? (?:today'?s? )?date(?: today)?"
    r"|(?:aaj|आज)\s+(?:kya|क्या|kaunsi|कौनसी)?\s*(?:date|तारीख|tareekh)\s*"
    r"(?:hai|है)?|date batao|डेट बताओ)\s*[?.!]*$",
    re.IGNORECASE)

# Read-only lookup questions: still the model's job (they need tools and
# phrasing), but not the big model's. Route to the fast model.
_SIMPLE_HINT = re.compile(
    r"(?:how many|kitne|कितने|kitni|कितनी|what(?:'s| is| are)?|kya|क्या|"
    r"kab|कब|kaun|कौन|check|batao|बताओ|dikhao|दिखाओ|read|padho|पढ़ो|sunao|सुनाओ)",
    re.IGNORECASE)
_SIMPLE_NOUN = re.compile(
    r"(?:email|mail|ईमेल|मेल|inbox|calendar|कैलेंडर|meeting|मीटिंग|"
    r"schedule|chat|message|मैसेज|weather|मौसम|unread)",
    re.IGNORECASE)
_COMPLEX_MARK = re.compile(
    r"(?:send|bhej|भेज|draft|likho|लिखो|create|banao|बनाओ|delete|hatao|हटाओ|"
    r"cancel|reply|jawab|जवाब|forward|schedule kar|attach|invite|"
    r"summari[sz]e|compare|find out why|figure|analy[sz]e|and then|"
    r"aur (?:phir|fir)|और फिर)",
    re.IGNORECASE)


def _clean_person(raw: str) -> str | None:
    person = " ".join(raw.strip(" ,.").split())
    if not _NAME_OK.match(person):
        return None
    if any(w.lower() in _NAME_STOPWORDS for w in person.split()):
        return None
    return person


def match(text: str) -> dict | None:
    """A fully-recognized command, or None (meaning: the model's job)."""
    text = " ".join((text or "").split()).strip(" .!")
    if not text:
        return None
    hindi = bool(_DEVANAGARI.search(text))
    if _TIME_PATTERNS.match(text):
        return {"kind": "time", "hindi": hindi}
    if _DATE_PATTERNS.match(text):
        return {"kind": "date", "hindi": hindi}
    if _UNREAD_PATTERNS.match(text):
        return {"kind": "unread_count", "hindi": hindi}
    if _LATEST_EMAIL_PATTERNS.match(text):
        return {"kind": "latest_email", "hindi": hindi}
    cal = _CALENDAR_PATTERNS.match(text)
    if cal:
        return {"kind": "calendar_peek", "day_offset": _day_offset(cal),
                "hindi": hindi}
    for pattern in _CHAT_SEND_PATTERNS:
        m = pattern.match(text)
        if not m:
            continue
        person = _clean_person(m.group("person"))
        body = m.group("body").strip()
        if person and body:
            return {"kind": "chat_send", "person": person, "body": body,
                    "hindi": bool(_DEVANAGARI.search(text))}
    return None


def wants_fast_model(text: str) -> bool:
    """True for read-only lookup questions that need the model but not the
    big one. Anything composing, changing, or chaining stays on the main
    model — a misrouted lookup answers slightly plainer; a misrouted
    multi-step task answers worse, so the complex markers win."""
    text = text or ""
    if _COMPLEX_MARK.search(text):
        return False
    return bool(_SIMPLE_HINT.search(text) and _SIMPLE_NOUN.search(text))


def answer_time(hindi: bool, now: datetime | None = None) -> str:
    now = now or datetime.now()
    spoken = now.strftime("%I:%M %p").lstrip("0")
    if hindi:
        return f"अभी {now.strftime('%H:%M')} बजे हैं।"
    return f"It's {spoken}."


def answer_date(hindi: bool, now: datetime | None = None) -> str:
    now = now or datetime.now()
    if hindi:
        return f"आज {now.strftime('%d %B %Y')}, {now.strftime('%A')} है।"
    return f"Today is {now.strftime('%A, %d %B %Y')}."
