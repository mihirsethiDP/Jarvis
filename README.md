# Jarvis — Internal Voice Assistant

Jarvis is a voice assistant that runs on employees' Windows machines and helps
with daily operations: finding and reading documents (local and Google Drive),
Gmail (read/send/organize), Google Chat, Calendar, looking up colleagues in
the company directory, and consulting the company's approved external AI
tools and internal systems — **always with the user's explicit permission,
scoped to their own access level, and with every action audited.**

Say **"Hey Jarvis"**, ask for what you need, and confirm anything that has a
side effect before it happens.

> The visual orb UI is inspired by [Ultron by Sagar Tamang](https://github.com/SAGAR-TAMANG/ultron-by-sagar-builds).

---

## How it works

```
 "Hey Jarvis"          what you said             the brain                  actions
┌──────────────┐   ┌──────────────────┐   ┌────────────────────┐   ┌──────────────────────┐
│ openWakeWord │ → │ mic + endpointing │ → │ Claude (tool loop) │ → │ Drive · Gmail · files │
│ (offline)    │   │ faster-whisper    │   │ claude-opus-4-8    │   │ external AI tools     │
└──────────────┘   │ (offline STT)     │   └────────────────────┘   └──────────┬───────────┘
                   └──────────────────┘              ↑                          │
                                                     │      every action passes through
                                          ┌──────────┴───────────────────────────────────┐
                                          │  SECURITY LAYER                              │
                                          │  permissions → confirmation → audit log      │
                                          └──────────────────────────────────────────────┘
                                                     ↓
                                            SAPI text-to-speech (offline)
```

Wake word detection and speech-to-text run **fully offline** on the employee's
machine. The only data that leaves the machine is (a) the conversation sent to
the Claude API, and (b) the actions the user explicitly approves (Drive, Gmail,
configured AI tools).

## Security model (short version)

| Layer | What it does |
|---|---|
| **Install-time consent** | `jarvis setup` asks the employee, per capability: allow / ask at first use / **deny**. Denied tools are removed from the model's toolset entirely and never asked about again. |
| **Capability grants** | First use of Drive / Gmail / files / each AI & internal tool asks the user: allow once, for this session, or always. Answers understood in English/Hindi/Hinglish; unrecognized answers **fail closed**. Grants are reviewable and revocable. |
| **Per-user identity** | Every integration authenticates as the individual employee (their OAuth token, their internal-tool token). Access levels are enforced **server-side** by each tool — one employee's Jarvis can never see another employee's data, and there is no shared service account to abuse. |
| **Confirmation** | Every side effect (send email, write/upload file) reads a concrete summary aloud and requires an explicit *yes* — even with an "always" grant. |
| **Audit log** | Every tool call, permission decision, and confirmation is appended to a SHA-256 hash-chained JSONL log. `jarvis audit --verify` detects tampering. |
| **Path allowlist** | Local file access is confined to configured directories; traversal and symlink escapes are resolved before checking. |
| **Prompt-injection boundary** | Content fetched from documents/Drive/AI tools is wrapped as untrusted data; the system prompt forbids following instructions found inside it — and side effects still require the *human's* confirmation regardless. |
| **Secret storage** | API keys live in Windows Credential Manager; Google OAuth tokens in DPAPI-encrypted files. Nothing sensitive in plaintext config. |

Full details, threat model, and IT rollout notes: [docs/SECURITY.md](docs/SECURITY.md).

## Quickstart (developer machine)

```powershell
# 1. Python 3.11+ (3.11 is the safest pin for the voice stack)
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1

# 2. Install — core + voice + local status UI + tests
pip install -e .[voice,ui,dev]

# 3. Claude API key (stored in Windows Credential Manager)
jarvis secrets set anthropic

# 4. Try it without a microphone first
jarvis --text

# 5. Full voice mode (first run downloads the wake-word + Whisper models)
jarvis
```

### Google Workspace (Drive, Gmail, Chat, Calendar, Directory)

1. In Google Cloud Console (project owned by the company Workspace), create an
   OAuth client of type **Desktop app**, set the consent screen to
   **Internal** (internal apps skip Google's verification review), and enable
   the **Drive**, **Gmail**, **Google Chat**, **Calendar**, and **People**
   APIs.
2. Point `google.credentials_file` in your config at the downloaded client
   secrets JSON.
3. Run `jarvis setup-google` — a browser opens; the employee signs in with
   their company account. Jarvis never sees the password; the token is stored
   DPAPI-encrypted for that Windows user only. **One consent covers every
   Google app at once.**
4. **Only for colleague lookup ("find X's email")**: a Workspace admin must
   set Admin console → Directory → Directory settings → Sharing settings →
   **External Directory Sharing** to "Organization data and authenticated
   user basic profile fields". Without this, lookups return empty no matter
   how correctly everything else is set up — Drive/Gmail/Chat/Calendar need
   no equivalent admin step.

Scopes are declared in `jarvis/defaults.yaml`: `drive.file` + `drive.readonly`
for Drive; `gmail.modify` for Gmail (read + send + archive/label/trash —
deliberately excludes permanent delete and Settings); narrow Chat scopes
(`chat.spaces.readonly` + `chat.messages.readonly` + `chat.messages.create`);
`calendar.readonly` + `calendar.events` for Calendar (covers free/busy too);
`directory.readonly` for colleague lookup.

**If employees already ran `jarvis setup-google` before this scope list grew**,
each of them needs to run it once more — expanding scopes doesn't retroactively
upgrade an already-issued token. Jarvis detects the mismatch itself and tells
the employee to re-authorize rather than failing mysteriously.

### External AI tools

List approved tools under `ai_tools:` in the config (OpenAI-compatible or
Anthropic-style endpoints). Jarvis can only reach tools on that list, the
user must grant `ai:<name>`, and every forwarded prompt is confirmed aloud.

### Internal company tools

Declare each internal tool's API under `internal_tools:` in the config — name,
base URL, and a whitelist of actions (read or write) with their parameters.
Employees connect with a **personal** token (`jarvis secrets set
tool-<name>-token`); Jarvis calls the API as that employee, so the tool's own
server-side permissions bound what Jarvis can do. Write actions are confirmed
aloud per action. Jarvis never receives database credentials — see
[docs/SECURITY.md](docs/SECURITY.md) for why that's the load-bearing decision.

### Hindi / Hinglish

Two config keys enable it: `audio.stt.language: auto` (or `hi`) for
understanding, and `audio.tts.engine: edge` for natural Hindi/Indian-English
neural voices (`hi-IN-SwaraNeural` / `en-IN-NeerjaNeural`). Consent and
confirmation answers ("haan", "नहीं", "theek hai"…) are understood either way.
Note: edge-tts synthesizes in Microsoft's cloud — the offline SAPI voice stays
the default and the automatic fallback.

## Commands

| Command | Purpose |
|---|---|
| `jarvis` | Start the assistant (voice if available, else text) |
| `jarvis --text` | Console chat mode |
| `jarvis --ui` | Also serve the local orb status page (127.0.0.1 only) |
| `jarvis setup` | Consent wizard: choose allow/ask/deny per capability |
| `jarvis setup-google` | Run Google authorization now |
| `jarvis secrets set anthropic` | Store the Claude API key in the keyring |
| `jarvis permissions list` / `revoke <cap>` | Review / revoke standing grants |
| `jarvis audit -n 50` / `--verify` | Inspect / verify the audit log |

## Configuration

Defaults live in [jarvis/defaults.yaml](jarvis/defaults.yaml); per-user overrides
go in `%APPDATA%\Jarvis\config.yaml` (same structure, only changed keys).
Notable settings: allowed file directories, wake-word threshold, Whisper model
size, TTS voice, the Claude model/effort, and the approved AI-tool list.

## Deployment notes (IT)

- **Employee installs**: `powershell -File scripts\install.ps1` copies the app
  to `%LOCALAPPDATA%\Jarvis`, builds its venv, runs the consent wizard and the
  API-key prompt, and registers per-user autostart. `scripts\uninstall.ps1`
  reverses it. (`scripts\setup.ps1` remains the developer-machine setup.)
- Run Jarvis **in the user session** (Startup shortcut or HKCU Run key →
  `pythonw.exe`). Never as a Windows service — session-0 isolation breaks
  microphone access.
- On proxy-locked networks, pre-bundle the model files (openWakeWord `.onnx`,
  faster-whisper) and point the config at local paths; the first-run
  downloaders are blocked by most corporate proxies.
- The status page and Google OAuth loopback bind to `127.0.0.1` only; expect
  one-time Windows Firewall prompts.
- Workspace admins using API access controls must mark the internal OAuth
  client as trusted, or employees will see "app blocked".

## What each Google integration can do

| App | Read | Write (always confirmed aloud) |
|---|---|---|
| **Drive** | search + read your files | create/upload files |
| **Gmail** | search + read your inbox | send · archive/label/trash (incl. mark read/unread) |
| **Chat** | list spaces/DMs + read messages in them | send a plain-text message |
| **Calendar** | view events + check free/busy (yours or a colleague's — busy/free only, never event details) | create/update/delete events (attendees get emailed) |
| **Directory** | look up a colleague's email/phone by name | — |

## Project layout

```
jarvis/
  app.py              orchestrator (wake → listen → think → act → speak)
  brain/              Claude agent loop + system prompt
  audio/              microphone, wake word, endpointing, STT, TTS
  tools/              Drive, Gmail, local files, AI-tool bridge
  security/           permissions, confirmation, audit chain, DPAPI, keyring
  integrations/       Google OAuth
  ui/                 local orb status page (FastAPI + WebSocket)
  defaults.yaml       defaults (user overrides in %APPDATA%\Jarvis)
docs/SECURITY.md      threat model & rollout guidance
tests/                security-layer test suite
```

## Roadmap

- System tray icon + toast notifications (pystray / Windows-Toasts)
- Push-to-talk hotkey fallback (Win32 `RegisterHotKey`)
- Follow-up conversation window (no wake word needed for ~8 s after a reply)
- Streaming replies — speak the first sentence while the rest generates
- Custom wake model for bare "Jarvis" (openWakeWord training notebook)
- Packaged installer (PyInstaller `--onedir` + signing) for managed rollout
