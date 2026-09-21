"""The Jarvis application: wires audio, brain, security, and UI together."""

from __future__ import annotations

import os
import sys
import threading
from datetime import datetime

from .audio import chime
from .brain import JarvisAgent
from .config import Config
from .io_channel import IOChannel, SwitchableIO, TextIO, VoiceIO, WebIO
from .memory import MemoryStore
from .paths import cli_hint
from .security import AuditLog, Confirmer, PermissionManager
from .security.limits import ActionLimiter
from .security import secrets as secret_store
from . import tools as tools_pkg
from .tools import ToolContext, build_all_tools
from .usage import TurnBudget


def _headless_error(message: str) -> None:
    """Surface a fatal startup problem when there is no console (pythonw)."""
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "Jarvis failed to start", 0x10)
    except Exception:
        pass

_EXIT_PHRASES = {"shut down", "shutdown", "exit", "quit", "stop listening", "goodbye jarvis"}


def bootstrap_api_key() -> bool:
    """Load the Claude API key from the keyring if the env var isn't set."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return True
    key = secret_store.get_secret("anthropic-api-key")
    if key:
        os.environ["ANTHROPIC_API_KEY"] = key
        return True
    return False


class JarvisApp:
    def __init__(self, config: Config, *, force_text: bool = False, with_ui: bool = False):
        # Must run before JarvisAgent constructs anthropic.Anthropic(), which
        # snapshots ANTHROPIC_API_KEY from the environment at that moment.
        bootstrap_api_key()
        self.config = config
        self.audit = AuditLog(anchored=True)
        self.state_server = None
        self._stop = threading.Event()
        # Serialises turns: the voice loop and a typed request must never run
        # the agent at the same time, or they share one conversation history.
        # A plain Lock, not an RLock — nothing here nests, and RLock has no
        # .locked() before Python 3.14, which is what tells the page we are busy.
        self._agent_lock = threading.Lock()
        self._push_to_talk = threading.Event()
        # A spoken question is mirrored to the HUD as Yes/No buttons; a click
        # sets this event, aborts the microphone wait, and its answer wins.
        self._ui_answer: str | None = None
        self._ui_answer_event = threading.Event()
        self._voice_ask_pending = False
        self._web_io = None
        self.overlay = None

        if with_ui or config.get("ui.enabled", False):
            try:
                from .ui.server import StateServer

                # Loopback only, by design — the page shows live conversation
                # state and must never be reachable from the LAN.
                from .audio.wakewords import spoken_phrase as _phrase

                self.state_server = StateServer(
                    port=int(config.get("ui.port", 8763)),
                    wake_phrase=_phrase(str(config.get("audio.wake.model", "hey_jarvis"))),
                    on_quit=self._quit_from_ui,
                    on_ask=self._ask_from_ui,
                    on_listen=self._listen_from_ui,
                    on_answer=self._answer_from_ui,
                )
                # Every gated action already flows through the audit log, so
                # subscribing here gives the live view complete coverage.
                self.audit.subscribe(self.state_server.record_activity)
                self.state_server.start()

                # The always-there piece. Nobody keeps a browser window open
                # all day, so without this Jarvis is invisible for most of the
                # day it is meant to be running.
                if config.get("ui.overlay", True):
                    from .audio.wakewords import spoken_phrase
                    from .ui.overlay import Overlay

                    overlay = Overlay(port=int(config.get("ui.port", 8763)),
                                      on_listen=self._listen_from_ui,
                                      # Yes/No on the widget answers the same
                                      # pending question the HUD's card does.
                                      on_answer=self._answer_from_ui,
                                      wake_phrase=spoken_phrase(
                                          str(config.get("audio.wake.model", "hey_jarvis"))))
                    self.overlay = overlay if overlay.start() else None
            except ImportError:
                print("UI dependencies missing — run `pip install .[ui]`. Continuing without UI.")

        self.voice = None
        if not (force_text or config.text_mode):
            self.voice = self._try_build_voice()
            if self.voice is None:
                print("Voice dependencies unavailable — falling back to text mode. "
                      "Install them with `pip install .[voice]`.")

        # A typed request is answered on the page; a spoken one out loud.
        server = self.state_server
        self._web_io = WebIO(
            publish_say=(server.publish_message if server is None
                         else (lambda t: server.publish_message("jarvis", t))),
            publish_prompt=(lambda p: None) if server is None else server.publish_prompt,
        )
        io = SwitchableIO(self._build_io())
        self.permissions = PermissionManager(
            io, self.audit, session_grant_minutes=config.session_grant_minutes,
            on_status=self._publish,
        )
        # Side-effect confirmation cannot be switched OFF — "smart" mode only
        # lets a tool vouch for a specific low-blast-radius action (internal
        # chat DM, confidently resolved), and even then never in a turn that
        # read untrusted content. Blast-radius caps: even a confirmed action
        # can't run away.
        self.confirmer = Confirmer(
            io, self.audit, limiter=ActionLimiter(), on_status=self._publish,
            mode=str(config.get("security.confirm_mode", "always")),
            untrusted_check=tools_pkg.turn_saw_untrusted,
        )
        self.memory = MemoryStore()
        limit = int(config.get("brain.daily_turn_limit", 200))
        self.turn_budget = TurnBudget(limit) if limit > 0 else None
        ctx = ToolContext(
            config=config, permissions=self.permissions,
            confirmer=self.confirmer, audit=self.audit, memory=self.memory,
            turn_budget=self.turn_budget,
            preview=(None if self.state_server is None
                     else self.state_server.publish_draft),
            clear_preview=(None if self.state_server is None
                           else self.state_server.clear_draft),
        )
        if (self.permissions.denied("memory_recall")
                and not self.permissions.denied("memory_write")):
            print("Note: you denied memory recall but allowed remembering — Jarvis "
                  "will store facts it never uses. Consider denying both, or "
                  f"allowing recall: {cli_hint('setup')}")
        self._ctx = ctx
        all_tools = build_all_tools(ctx)
        # For the intent fast-path: direct access to the same gated tools the
        # model uses. Never a new capability — just a shorter route to them.
        self._tool_map = {t.name: t for t in all_tools}
        self.agent = JarvisAgent(
            config, all_tools, self.audit, on_status=self._publish,
            on_narrate=self._narrate,
            memory=self.memory,
            # Asked once per session, and only when memory is non-empty, so a
            # standing denial blocks injection and "ask" genuinely asks.
            recall_check=lambda: self.permissions.require(
                "memory_recall", "use what it remembered about you earlier"
            ),
            turn_budget=self.turn_budget,
        )
        self.agent.denied_capabilities = getattr(ctx, "denied_capabilities", [])
        self.io: IOChannel = io

    # ------------------------------------------------------------------
    def _publish(self, state: str, detail: str = "") -> None:
        if self.state_server is not None:
            self.state_server.publish(state, detail)
        if self.overlay is not None:
            self.overlay.publish(state, detail)

    def _quit_from_ui(self) -> None:
        """Quit button on the status page. The voice loop notices within a
        second; the watchdog covers the case where it is wedged in a blocking
        audio call and would otherwise leave a process with no window."""
        self.audit.record("shutdown", detail="quit from status page", decision="ui")
        self._stop.set()
        threading.Timer(4.0, lambda: os._exit(0)).start()

    # -- requests arriving from the status page ---------------------------
    def _ui_busy(self) -> bool:
        return self._agent_lock.locked()

    def _ask_from_ui(self, text: str) -> bool:
        """A typed request. Runs on a worker so the HTTP call returns at once.

        Refused rather than queued while a turn is already running: two turns
        interleaved would share one conversation history and one microphone.
        """
        if self._ui_busy():
            return False
        threading.Thread(target=self._run_ui_turn, args=(text,),
                         daemon=True, name="jarvis-ui-turn").start()
        return True

    def _run_ui_turn(self, text: str) -> None:
        with self._agent_lock:
            server = self.state_server
            if server is not None:
                server.publish_message("you", text)
            # Route this turn's permission and confirmation questions to the
            # page. Someone who typed may have done so precisely because they
            # cannot speak, so asking them out loud would strand the request.
            self.io.use(self._web_io)
            self.permissions.begin_turn()
            tools_pkg.begin_turn()
            try:
                # Typed requests take the same fast lane and model routing as
                # spoken ones; confirmations land on the page either way.
                reply = self._try_fastpath(text)
                if reply is None:
                    from .brain import fastpath as _fastpath

                    fast_model = str(self.config.get("brain.fast_model", "") or "")
                    model = (fast_model if fast_model
                             and _fastpath.wants_fast_model(text) else None)
                    reply = self.agent.run_turn(text, model=model)
            except Exception as e:
                self.audit.record("error", tool="ui_turn", detail=str(e), ok=False)
                reply = "Something went wrong handling that — please try again."
            finally:
                self.io.use(None)
                if server is not None:
                    server.clear_prompt()
            if server is not None:
                server.publish_message("jarvis", reply)
            if self.overlay is not None and reply:
                self.overlay.publish_message(reply)
            self._publish("idle")

    def _listen_from_ui(self) -> bool:
        """Push-to-talk: record one utterance with no wake word."""
        if self.voice is None or self._ui_busy():
            return False
        self._push_to_talk.set()
        return True

    def _answer_from_ui(self, answer: str) -> None:
        if self._voice_ask_pending:
            # The question was asked aloud; the user clicked instead of
            # speaking. No one has time to dictate "yes" to a screen that
            # has a Yes button on it.
            self._ui_answer = answer
            self._ui_answer_event.set()
        else:
            self._web_io.deliver(answer)

    def _narrate(self, phrase: str) -> None:
        """Say aloud what Jarvis is about to do.

        Tool calls are where a turn's seconds go, and until now the user
        finished speaking and then heard nothing at all until the whole thing
        was done. Narration is best-effort: a TTS hiccup must never take down
        the turn it was only describing.
        """
        voice = self.voice
        if not voice:
            return
        try:
            voice["speaker"].say(phrase)
        except Exception:
            pass

    def _note_voice_engine(self, engine: str, reason: str) -> None:
        """Surface a change of speaking voice on the status page.

        Without this the accent silently switches to US English mid-session
        and looks like a broken setting rather than a network problem.
        """
        # Through the audit log rather than straight to the page: the feed
        # gets it via the subscription either way, and it persists — so "it
        # sounded robotic earlier" is answerable after the fact instead of
        # being lost when the page closes.
        self.audit.record(
            "voice",
            tool="Indian voice" if engine == "edge" else "offline voice (US English)",
            detail=reason,
            decision="restored" if engine == "edge" else "degraded",
            ok=engine == "edge",
        )

    def _try_build_voice(self):
        try:
            from .audio.microphone import Microphone
            from .audio.recorder import UtteranceRecorder
            from .audio.stt import Transcriber
            from .audio.tts import Speaker
            from .audio.wake import WakeWordDetector
        except ImportError:
            return None
        mic = None
        try:
            cfg = self.config
            mic = Microphone(
                sample_rate=int(cfg.get("audio.sample_rate", 16000)),
                device=cfg.get("audio.input_device"),
            )
            mic.start()
            print("Loading models (wake word + speech recognition)…")
            wake = WakeWordDetector(
                mic,
                model_name=str(cfg.get("audio.wake.model", "hey_jarvis")),
                threshold=float(cfg.get("audio.wake.threshold", 0.5)),
            )
            # Voice-activity detection, when the model loads. Falls back to
            # RMS energy, which cannot tell a fan from a voice.
            from .audio.vad import try_build as _build_vad

            detector = _build_vad(bool(cfg.get("audio.vad.enabled", True)))
            recorder = UtteranceRecorder(
                mic,
                detector=detector,
                max_seconds=float(cfg.get("audio.stt.max_seconds", 20)),
                silence_seconds=float(cfg.get("audio.stt.silence_seconds", 1.8)),
                min_speech_seconds=float(cfg.get("audio.stt.min_speech_seconds", 0.3)),
            )
            stt = Transcriber(
                model_size=str(cfg.get("audio.stt.model_size", "base.en")),
                compute_type=str(cfg.get("audio.stt.compute_type", "int8")),
                language=str(cfg.get("audio.stt.language", "en")),
                expected_languages=tuple(
                    cfg.get("audio.stt.expected_languages", ["en", "hi"]) or ["en", "hi"]),
            )
            # Language detection starts on the opening seconds of speech
            # while the user is still talking — by the time the recording
            # ends, the language answer is already waiting (~1.7s saved on
            # the serial path of every utterance).
            recorder.on_early_audio = stt.begin_early_detection
            offline_speaker = Speaker(
                voice=cfg.get("audio.tts.voice"),
                rate=int(cfg.get("audio.tts.rate", 180)),
            )
            engine = str(cfg.get("audio.tts.engine", "pyttsx3"))
            if engine not in ("pyttsx3", "edge"):
                print(f"Unknown audio.tts.engine '{engine}' — using pyttsx3.")
            if engine == "edge":
                from .audio.tts_edge import EdgeSpeaker

                speaker = EdgeSpeaker(
                    voice_en=str(cfg.get("audio.tts.edge_voice_en", "en-IN-NeerjaNeural")),
                    voice_hi=str(cfg.get("audio.tts.edge_voice_hi", "hi-IN-SwaraNeural")),
                    fallback=offline_speaker,
                    on_engine=self._note_voice_engine,
                )
            else:
                speaker = offline_speaker
            return {"mic": mic, "wake": wake, "recorder": recorder,
                    "stt": stt, "speaker": speaker}
        except Exception as e:
            if mic is not None:
                mic.stop()  # don't leave the stream capturing in text mode
            print(f"Voice pipeline failed to start ({e}).")
            return None

    def _build_io(self) -> IOChannel:
        if self.voice is None:
            return TextIO()
        v = self.voice

        def listen() -> str:
            v["mic"].drain()
            audio = v["recorder"].record()
            return v["stt"].transcribe(audio)

        def listen_for_answer() -> str:
            self._ui_answer = None
            self._ui_answer_event.clear()
            self._voice_ask_pending = True
            try:
                v["mic"].drain()
                audio = v["recorder"].record(abort_event=self._ui_answer_event)
                if self._ui_answer is not None:
                    return self._ui_answer
                heard = v["stt"].transcribe(audio)
                return self._ui_answer if self._ui_answer is not None else heard
            finally:
                self._voice_ask_pending = False

        def listen_short() -> str:
            from .security.confirm import _NO_WORDS, _is_yes
            from .security.permissions import normalize_answer

            def clear_answer(text: str) -> bool:
                normalized = normalize_answer(text)
                return _is_yes(normalized) or any(
                    w in _NO_WORDS for w in normalized.split())

            # Race the microphone against the HUD Yes/No buttons.
            self._ui_answer = None
            self._ui_answer_event.clear()
            self._voice_ask_pending = True
            try:
                v["mic"].drain()
                audio = v["recorder"].record(
                    start_window=8, abort_event=self._ui_answer_event)
                if self._ui_answer is not None:
                    return self._ui_answer
                heard = v["stt"].transcribe_quick(audio, recognizer=clear_answer)
                # A click that landed while the decode ran beats a mumble.
                return self._ui_answer if self._ui_answer is not None else heard
            finally:
                self._voice_ask_pending = False

        def speak(text: str) -> None:
            v["speaker"].say(text)
            v["mic"].drain()

        def prompt_out(prompt: str) -> None:
            if self.state_server is not None:
                self.state_server.publish_prompt(prompt)
            if self.overlay is not None:
                self.overlay.publish_prompt(prompt)

        def prompt_done() -> None:
            if self.state_server is not None:
                self.state_server.clear_prompt()
            if self.overlay is not None:
                self.overlay.clear_prompt()

        return VoiceIO(
            speak=speak, listen=listen_for_answer, listen_short=listen_short,
            on_prompt=prompt_out, on_prompt_done=prompt_done,
        )

    # ------------------------------------------------------------------
    def run(self) -> None:
        if not bootstrap_api_key():
            message = (
                "No Claude API key found. Set the ANTHROPIC_API_KEY environment "
                f"variable, or store it securely with:  {cli_hint('secrets set anthropic')}"
            )
            self.audit.record("startup", detail="no API key", decision="failed", ok=False)
            if sys.stdin is None:
                _headless_error(message)  # autostart must not die invisibly
            else:
                print(message)
            return
        from .setup_wizard import setup_marker_exists

        if not setup_marker_exists():
            print(
                "Tip: you haven't run the consent wizard yet — it lets you choose "
                f"exactly what Jarvis may access:  {cli_hint('setup')}\n"
                "Until then, Jarvis asks at first use of each capability."
            )
        self.audit.record("startup", detail="voice" if self.voice else "text")
        threading.Thread(target=self._warm_up, daemon=True,
                         name="assistant-warmup").start()
        try:
            if self.voice is not None:
                self._run_voice()
            else:
                self._run_text()
        finally:
            if self.overlay is not None:
                self.overlay.close()
                # Tk was created on a worker thread; letting the interpreter
                # finalize with it still around makes Tcl abort the process
                # with exit code 3. Every audit record is already fsynced, so
                # there is nothing left to flush.
                sys.stdout.flush()
                os._exit(0)

    def _run_text(self) -> None:
        if sys.stdin is None:
            # Launched via pythonw.exe (autostart) but voice never came up:
            # there is no console to fall back to. Fail loudly, not silently.
            self.audit.record("startup", detail="voice unavailable and no console",
                              decision="failed", ok=False)
            _headless_error(
                "Jarvis could not start voice mode and has no console for text "
                f"mode. Run  {cli_hint('--text')}  from a terminal to diagnose."
            )
            return
        name = self.config.get("assistant.name", "Jarvis")
        print(f"\n{name} (text mode) — type your request, or 'exit' to quit.")
        self._publish("idle")
        while True:
            try:
                text = input("\nYou: ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not text:
                continue
            if text.lower() in _EXIT_PHRASES:
                break
            self._publish("thinking", text)
            reply = self.agent.run_turn(text)
            self._publish("speaking", reply)
            print(f"\nJarvis: {reply}")
            self._publish("idle")
        print("\nGoodbye.")

    def _speak_turn(self, text: str) -> bool:
        with self._agent_lock:
            return self._speak_turn_locked(text)

    def _speak_turn_locked(self, text: str) -> bool:
        """Run one brain turn and speak the reply. Returns False on shutdown."""
        # "Allow once" covers this request, not one tool call inside it.
        self.permissions.begin_turn()
        tools_pkg.begin_turn()
        v = self.voice
        if text.lower().strip(" .!,") in _EXIT_PHRASES:
            v["speaker"].say("Shutting down. Goodbye.")
            return False
        server = self.state_server
        if server is not None:
            server.publish_message("you", text)
        self._publish("thinking", text)
        reply = self.agent.run_turn(text)
        self._publish("speaking", reply)
        if server is not None:
            server.publish_message("jarvis", reply)
        print(f"Jarvis: {reply}")
        v["speaker"].say(reply)
        v["mic"].drain()
        return True

    def _warm_up(self) -> None:
        """Pay every first-use cost up front, off the critical path.

        Without this the FIRST request of the day carries: the tiny detector
        model load (~2s), cold ctranslate2 kernels, two Google API discovery
        builds (~1s each), the directory fetch (~1s), and the cloud TTS
        round-trip for the confirmation phrasing (~1.8s). The worst turn an
        employee experiences should not be their first one.
        """
        import numpy as np

        try:
            if self.voice is not None:
                # Loads the tiny detector and warms the decode kernels.
                self.voice["stt"].transcribe(np.zeros(8000, dtype=np.float32))
        except Exception:
            pass
        try:
            if self.voice is not None and hasattr(self.voice["speaker"], "prewarm"):
                # The fixed sentences of the consent gates: cached now, every
                # confirmation after this plays them instantly.
                self.voice["speaker"].prewarm([
                    "Say yes to proceed, or no to cancel.",
                    "Sorry, I didn't catch that. Say yes to go ahead, or no to cancel.",
                    "Sorry, I didn't catch that.",
                ])
        except Exception:
            pass
        try:
            from .tools import directory as _directory

            people = self._ctx.google_service("people", "v1")
            _directory._fetch_all(people)                      # directory cache
            self._ctx.google_service("gmail", "v1").users().getProfile(
                userId="me").execute()                         # own domain
        except Exception:
            pass   # not signed in yet, or offline — first use pays as before

    def _try_fastpath(self, text: str) -> str | None:
        """Execute a fully-recognized command without the LLM, or None.

        None means "not my job" — the caller hands the text to the model
        exactly as before. Every side effect still runs through the same
        permission/confirmation-gated tools; this only removes the model
        round trip, never a check.
        """
        from .brain import fastpath

        intent = fastpath.match(text)
        if intent is None:
            return None
        try:
            if intent["kind"] == "time":
                return fastpath.answer_time(intent["hindi"])
            if intent["kind"] == "date":
                return fastpath.answer_date(intent["hindi"])
            if intent["kind"] == "chat_send":
                return self._fastpath_chat_send(intent)
            from .tools import quick

            if intent["kind"] == "unread_count":
                self._narrate("Checking your inbox")
                return quick.unread_count(self._ctx, intent["hindi"])
            if intent["kind"] == "latest_email":
                self._narrate("Reading your latest email")
                return quick.latest_email(self._ctx, intent["hindi"])
            if intent["kind"] == "calendar_peek":
                self._narrate("Checking your calendar")
                return quick.events_for_day(self._ctx, intent["day_offset"],
                                            intent["hindi"])
        except Exception:
            self.audit.record("turn", tool="fastpath",
                              detail=f"error on {intent['kind']} — fell through",
                              ok=False)
            return None
        return None

    def _fastpath_chat_send(self, intent: dict) -> str | None:
        import re as _re

        find = self._tool_map.get("find_direct_message")
        send = self._tool_map.get("send_chat_message")
        if find is None or send is None:
            return None    # capability denied at setup: the model explains
        self._narrate(f"Finding {intent['person']}'s chat")
        found = find(intent["person"])
        space = _re.search(r"space id (spaces/\S+?)\.", found + ".")
        if space is None:
            # Ambiguous, unknown, or no DM — the model handles the follow-up
            # conversation better than a template can.
            self.audit.record("turn", tool="fastpath",
                              detail=f"chat_send: unresolved '{intent['person']}'")
            return None
        sent = send(space.group(1), intent["body"])
        recipient = _re.search(r"Message sent to (.+?):", sent)
        if recipient is not None:
            who = recipient.group(1)
            self.audit.record("turn", tool="fastpath",
                              detail=f"chat_send -> {who}")
            return (f"{who} को भेज दिया।" if intent["hindi"]
                    else f"Sent to {who}.")
        if "Refused by the safety limiter" in sent:
            return ("Send limit reached for now — that one has to go "
                    "manually, or wait a bit.")
        if sent.startswith("Cancelled") and "They said:" not in sent:
            return "ठीक है, नहीं भेजा।" if intent["hindi"] else "Okay — not sent."
        # Declined WITH a correction, or an API error: the model's job.
        self.audit.record("turn", tool="fastpath",
                          detail="chat_send: handed to the model")
        return None

    def _run_voice(self) -> None:
        v = self.voice
        name = self.config.get("assistant.name", "Jarvis")
        # After a reply, keep listening briefly: people correct themselves
        # ("no wait, I misspoke") and shouldn't need the wake word mid-flow.
        follow_secs = float(self.config.get("audio.follow_up_seconds", 8))
        print(f'\n{name} is listening — say "Hey Jarvis". Ctrl+C to quit.')
        try:
            while True:
                # One bad turn (TTS hiccup, tool exception, STT failure) must
                # never take the whole assistant down.
                try:
                    self._publish("idle")
                    # Wake word, Quit, or the page's talk button — whichever
                    # comes first.
                    woke = v["wake"].wait(
                        should_stop=lambda: (self._stop.is_set()
                                             or self._push_to_talk.is_set())
                    )
                    if not woke:
                        if self._stop.is_set():
                            return      # Quit pressed
                        if not self._push_to_talk.is_set():
                            return
                        self._push_to_talk.clear()   # talk button: fall through
                        v["mic"].drain()
                    self._publish("listening")
                    # The cue lands before recording so the user knows the
                    # microphone is open. Without it there was no way to tell
                    # "I'm recording you" from "nothing happened".
                    chime.play("listening")
                    print("(wake word detected — listening…)")
                    audio = v["recorder"].record()
                    # The "done" tone plus a live caption ("heard 6s — writing
                    # it down") is the cue that the mic has closed and work
                    # has started; without it this stretch reads as dead.
                    chime.play("done")
                    self._publish("transcribing",
                                  f"heard {len(audio) / 16000:.0f}s — writing it down")
                    import time as _time
                    _t0 = _time.monotonic()
                    text = v["stt"].transcribe(audio)
                    self._turn_timing = {
                        "speech": round(getattr(v["recorder"], "last_speech_seconds", 0), 2),
                        "tail": round(getattr(v["recorder"], "last_tail_seconds", 0), 2),
                        "stt": round(_time.monotonic() - _t0, 2),
                        **{k: round(x, 2)
                           for k, x in getattr(v["stt"], "last_timing", {}).items()},
                    }
                    if not text:
                        v["speaker"].say("Sorry, I didn't catch that.")
                        v["mic"].drain()
                        continue
                    print(f"You: {text}")
                    if not self._speak_turn(text):
                        return
                    # Follow-up window: no wake word needed to continue.
                    while follow_secs > 0:
                        self._publish("listening", "still listening — just speak")
                        chime.play("listening")
                        audio = v["recorder"].record(start_window=follow_secs)
                        if audio.size == 0:
                            break  # genuine silence — back to the wake word
                        chime.play("done")
                        # The follow-up path never published this state, so
                        # the pill sat on "listening" while transcription ran.
                        self._publish("transcribing",
                                      f"heard {len(audio) / 16000:.0f}s — writing it down")
                        followup = v["stt"].transcribe(audio)
                        if not followup:
                            # Speech was captured but could not be transcribed.
                            # Dropping it silently looked like being ignored;
                            # say so and keep the window open.
                            v["speaker"].say("Sorry, I didn't catch that.")
                            v["mic"].drain()
                            continue
                        print(f"You: {followup}")
                        if not self._speak_turn(followup):
                            return
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    self.audit.record("error", tool="voice_loop", detail=str(e), ok=False)
                    print(f"(recovered from error: {e})")
                    try:
                        v["speaker"].say("Something went wrong with that one — try again.")
                        v["mic"].drain()
                    except Exception:
                        pass
        except KeyboardInterrupt:
            pass
        finally:
            v["mic"].stop()
            self._publish("offline")
        print("\nGoodbye.")
