# Jarvis — pilot rollout guide

For a **limited set of users sharing one company Claude API key**. Not a
general-availability guide; see the "Before wider rollout" section at the end
for what would need to change first.

Two parts: what IT prepares once, and what happens at each employee's machine.

---

## Once, before any install (IT / project owner)

- [ ] **Spend cap on the Anthropic workspace.** With a shared key this is the
      real ceiling — set it deliberately, not "later". See the arithmetic in
      *Cost with a shared key* below.
- [ ] **Decide the per-machine daily turn limit** (`brain.daily_turn_limit`,
      default 200; 100 on the pilot machine). Each machine enforces its own
      limit, so the fleet's worst case is `users × limit × cost-per-turn`.
- [ ] **Decide who gets `code_run`.** Recommended: deny for everyone in the
      pilot, enable later for specific people who need data crunching.
- [ ] **Have the shared API key ready** in whatever the company uses to
      distribute credentials — never in email or chat.
- [ ] **Confirm the Google side is done** (one-time, already complete for
      DigitalPaani): Cloud project with Drive/Gmail/Chat/Calendar/People APIs
      enabled, Internal consent screen, Desktop OAuth client, Chat API
      Configuration tab filled in, Admin console allowing third-party apps.

---

## Per employee (~10 minutes)

Sit with them for the first few; the wizard asks questions only they should
answer.

### 1. Install

From a copy of the repository, in PowerShell:

```
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
```

This copies Jarvis to their profile, builds its Python environment, then runs
the two interactive steps below. It registers Jarvis to start at login.

### 2. Consent wizard (the employee answers, not IT)

For each capability they answer **allow**, **ask**, or **deny**:

| Answer | Meaning |
|---|---|
| `allow` | Jarvis may use it. Side effects are *still* confirmed each time. |
| `ask` | Jarvis asks the first time it needs it. **Good default.** |
| `deny` | The tool is removed entirely. Jarvis never asks again. |

Pilot guidance: `ask` for most things, **`deny` for "Run code"**, and
`deny` for anything they know they won't use.

They can re-run the wizard anytime to change their answers.

### 3. Claude API key

The installer prompts for it (hidden input). Paste the shared key. It goes
into Windows Credential Manager — never a file, never plaintext.

### 4. Google authorization

```
"%LOCALAPPDATA%\Jarvis\app\.venv\Scripts\python.exe" -m jarvis setup-google
```

A browser opens; they sign in with **their own** company account and approve
once. **Every box must be ticked** — Jarvis detects a partial grant and
refuses rather than half-working.

### 5. Check it

```
... -m jarvis security-check    # should report 0 risks
... -m jarvis --text            # type a question, confirm it answers
... -m jarvis                   # full voice mode; say "Hey Jarvis"
```

First voice start downloads speech models (a few hundred MB) and takes a
couple of minutes. Later starts are quick.

---

## Cost with a shared key

Each machine enforces its own daily cap, so a shared key does **not** mean
uncapped aggregate spend. Worst case per day:

```
users  x  daily_turn_limit  x  cost per turn
```

At the measured rate (~₹2.50/turn on Sonnet) with 5 pilot users at 100
turns/day, the ceiling is about **₹1,250/day** — and real use runs far below
its cap. Set the Anthropic workspace spend limit near your intended monthly
number so the cap, not the arithmetic, is authoritative.

---

## What a shared key costs you (and the compensating controls)

One key for several people means **the API bill can't tell you who spent
what**. The pilot compensates:

- **Per-machine daily caps** bound each person independently.
- **The audit log is per-employee and local** (`jarvis audit`), hash-chained
  and anchored — so "who did what" is answerable on the machine even though
  the *bill* can't distinguish people.
- **Rotation is all-or-nothing.** If the key leaks, every pilot machine needs
  the new one. Keep the pilot small enough that this is an afternoon, not a
  project.

---

## Support basics

| Symptom | Fix |
|---|---|
| "No Claude API key found" | `-m jarvis secrets set anthropic` |
| "additional Google permissions… re-authorize" | `-m jarvis setup-google` (scopes grew) |
| "app blocked" during Google sign-in | Admin console → API controls → allow the OAuth client |
| Jarvis asks permission repeatedly | Expected for `ask`; answer "always allow" to make it stick |
| Wants to see what Jarvis did | `-m jarvis audit -n 30`, or the live view at `http://127.0.0.1:8763` |
| Wants to check nothing was tampered with | `-m jarvis audit --verify` |
| Wants to review/erase what it remembers | `-m jarvis memory list` / `forget <id>` / `clear` |

---

## Before wider rollout

Not blockers for a pilot, but revisit before going past a handful of users:

- **Per-employee API keys** — restores cost attribution and per-person
  revocation; the pilot deliberately trades these away for simplicity.
- **Signed installer** (PyInstaller `--onedir` + Authenticode) so corporate
  EDR doesn't quarantine it, distributed via Intune.
- **Central audit forwarding** — local logs are tamper-evident but live on
  the employee's machine; an IT-controlled sink makes them tamper-*proof*.
- **Model files pre-bundled** so proxy-locked machines don't each download
  hundreds of MB from Hugging Face and GitHub on first run.
