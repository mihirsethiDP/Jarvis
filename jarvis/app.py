"""The Jarvis application: wires audio, brain, security, and UI together."""

from __future__ import annotations

import os
import sys

from .brain import JarvisAgent
from .config import Config
from .io_channel import IOChannel, TextIO, VoiceIO
from .memory import MemoryStore
from .paths import cli_hint
from .security import AuditLog, Confirmer, PermissionManager
from .security import secrets as secret_store
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

        if with_ui or config.get("ui.enabled", False):
            try:
                from .ui.server import StateServer

                # Loopback only, by design — the page shows live conversation
                # state and must never be reachable from the LAN.
                self.state_server = StateServer(port=int(config.get("ui.port", 8763)))
                # Every gated action already flows through the audit log, so
                # subscribing here gives the live view complete coverage.
                self.audit.subscribe(self.state_server.record_activity)
                self.state_server.start()
            except ImportError:
                print("UI dependencies missing — run `pip install .[ui]`. Continuing without UI.")

        self.voice = None
        if not (force_text or config.text_mode):
            self.voice = self._try_build_voice()
            if self.voice is None:
                print("Voice dependencies unavailable — falling back to text mode. "
                      "Install them with `pip install .[voice]`.")

        io = self._build_io()
        self.permissions = PermissionManager(
            io, self.audit, session_grant_minutes=config.session_grant_minutes
        )
        # Side-effect confirmation is always on — deliberately not configurable,
        # so no config edit (or prompt-injected "helpful suggestion") can
        # disable the human-in-the-loop gate.
        self.confirmer = Confirmer(io, self.audit)
        self.memory = MemoryStore()
        limit = int(config.get("brain.daily_turn_limit", 200))
        self.turn_budget = TurnBudget(limit) if limit > 0 else None
        ctx = ToolContext(
            config=config, permissions=self.permissions,
            confirmer=self.confirmer, audit=self.audit, memory=self.memory,
            turn_budget=self.turn_budget,
        )
        if (self.permissions.denied("memory_recall")
                and not self.permissions.denied("memory_write")):
            print("Note: you denied memory recall but allowed remembering — Jarvis "
                  "will store facts it never uses. Consider denying both, or "
                  f"allowing recall: {cli_hint('setup')}")
        self.agent = JarvisAgent(
            config, build_all_tools(ctx), self.audit, on_status=self._publish,
            memory=self.memory,
            # Asked once per session, and only when memory is non-empty, so a
            # standing denial blocks injection and "ask" genuinely asks.
            recall_check=lambda: self.permissions.require(
                "memory_recall", "use what it remembered about you earlier"
            ),
            turn_budget=self.turn_budget,
        )
        self.io: IOChannel = io

    # ------------------------------------------------------------------
    def _publish(self, state: str, detail: str = "") -> None:
        if self.state_server is not None:
            self.state_server.publish(state, detail)

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
            recorder = UtteranceRecorder(
                mic,
                max_seconds=float(cfg.get("audio.stt.max_seconds", 20)),
                silence_seconds=float(cfg.get("audio.stt.silence_seconds", 1.2)),
            )
            stt = Transcriber(
                model_size=str(cfg.get("audio.stt.model_size", "base.en")),
                compute_type=str(cfg.get("audio.stt.compute_type", "int8")),
                language=str(cfg.get("audio.stt.language", "en")),
            )
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

        def speak(text: str) -> None:
            v["speaker"].say(text)
            v["mic"].drain()

        return VoiceIO(speak=speak, listen=listen)

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
        if self.voice is not None:
            self._run_voice()
        else:
            self._run_text()

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
        """Run one brain turn and speak the reply. Returns False on shutdown."""
        v = self.voice
        if text.lower().strip(" .!,") in _EXIT_PHRASES:
            v["speaker"].say("Shutting down. Goodbye.")
            return False
        self._publish("thinking", text)
        reply = self.agent.run_turn(text)
        self._publish("speaking", reply)
        print(f"Jarvis: {reply}")
        v["speaker"].say(reply)
        v["mic"].drain()
        return True

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
                    v["wake"].wait()
                    self._publish("listening")
                    print("(wake word detected — listening…)")
                    audio = v["recorder"].record()
                    self._publish("transcribing")
                    text = v["stt"].transcribe(audio)
                    if not text:
                        v["speaker"].say("Sorry, I didn't catch that.")
                        v["mic"].drain()
                        continue
                    print(f"You: {text}")
                    if not self._speak_turn(text):
                        return
                    # Follow-up window: no wake word needed to continue.
                    while follow_secs > 0:
                        self._publish("listening", "follow-up — just speak")
                        audio = v["recorder"].record(start_window=follow_secs)
                        followup = v["stt"].transcribe(audio)
                        if not followup:
                            break  # silence — back to waiting for the wake word
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
