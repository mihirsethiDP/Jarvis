# Jarvis — Internal Voice Assistant

Jarvis is a voice assistant that runs on employees' Windows machines and helps
with daily operations: finding and reading documents (local and Google Drive),
saving files, sending email, and consulting the company's approved external AI
tools — **always with the user's explicit permission, and with every action
audited.**

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
| **Capability grants** | First use of Drive / Gmail / files / each AI tool asks the user: allow once, for this session, or always. Unrecognized answers **fail closed**. Grants are reviewable and revocable. |
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

### Google Drive & Gmail

1. In Google Cloud Console (project owned by the company Workspace), create an
   OAuth client of type **Desktop app**, set the consent screen to
   **Internal** (internal apps skip Google's verification review), and enable
   the **Drive** and **Gmail** APIs.
2. Point `google.credentials_file` in your config at the downloaded client
   secrets JSON.
3. Run `jarvis setup-google` — a browser opens; the employee signs in with
   their company account. Jarvis never sees the password; the token is stored
   DPAPI-encrypted for that Windows user only.

Default scopes are least-privilege: `drive.file` + `drive.readonly` +
`gmail.send` (no mailbox read access). See `config/default.yaml`.

### External AI tools

List approved tools under `ai_tools:` in the config (OpenAI-compatible or
Anthropic-style endpoints). Jarvis can only reach tools on that list, and the
user must still grant `ai:<name>` at runtime.

## Commands

| Command | Purpose |
|---|---|
| `jarvis` | Start the assistant (voice if available, else text) |
| `jarvis --text` | Console chat mode |
| `jarvis --ui` | Also serve the local orb status page (127.0.0.1 only) |
| `jarvis setup-google` | Run Google authorization now |
| `jarvis secrets set anthropic` | Store the Claude API key in the keyring |
| `jarvis permissions list` / `revoke <cap>` | Review / revoke standing grants |
| `jarvis audit -n 50` / `--verify` | Inspect / verify the audit log |

## Configuration

Defaults live in [config/default.yaml](config/default.yaml); per-user overrides
go in `%APPDATA%\Jarvis\config.yaml` (same structure, only changed keys).
Notable settings: allowed file directories, wake-word threshold, Whisper model
size, TTS voice, the Claude model/effort, and the approved AI-tool list.

## Deployment notes (IT)

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
config/default.yaml   defaults (user overrides in %APPDATA%\Jarvis)
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
