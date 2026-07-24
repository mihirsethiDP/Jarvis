"""The Jarvis application: wires audio, brain, security, and UI together."""

from __future__ import annotations

import os

from .brain import JarvisAgent
from .config import Config
from .io_channel import IOChannel, TextIO, VoiceIO
from .security import AuditLog, Confirmer, PermissionManager
from .security import secrets as secret_store
from .tools import ToolContext, build_all_tools

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
        self.config = config
        self.audit = AuditLog()
        self.state_server = None

        if with_ui or config.get("ui.enabled", False):
            try:
                from .ui.server import StateServer

                self.state_server = StateServer(
                    host=str(config.get("ui.host", "127.0.0.1")),
                    port=int(config.get("ui.port", 8763)),
                )
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
        self.confirmer = Confirmer(io, self.audit, enabled=config.require_confirmation)
        ctx = ToolContext(
            config=config, permissions=self.permissions,
            confirmer=self.confirmer, audit=self.audit,
        )
        self.agent = JarvisAgent(
            config, build_all_tools(ctx), self.audit, on_status=self._publish
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
            )
            speaker = Speaker(
                voice=cfg.get("audio.tts.voice"),
                rate=int(cfg.get("audio.tts.rate", 180)),
            )
            return {"mic": mic, "wake": wake, "recorder": recorder,
                    "stt": stt, "speaker": speaker}
        except Exception as e:
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
            print(
                "No Claude API key found. Set the ANTHROPIC_API_KEY environment "
                "variable, or store it securely with:  jarvis secrets set anthropic"
            )
            return
        self.audit.record("startup", detail="voice" if self.voice else "text")
        if self.voice is not None:
            self._run_voice()
        else:
            self._run_text()

    def _run_text(self) -> None:
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

    def _run_voice(self) -> None:
        v = self.voice
        name = self.config.get("assistant.name", "Jarvis")
        print(f'\n{name} is listening — say "Hey Jarvis". Ctrl+C to quit.')
        try:
            while True:
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
                if text.lower().strip(" .!,") in _EXIT_PHRASES:
                    v["speaker"].say("Shutting down. Goodbye.")
                    break
                self._publish("thinking", text)
                reply = self.agent.run_turn(text)
                self._publish("speaking", reply)
                print(f"Jarvis: {reply}")
                v["speaker"].say(reply)
                v["mic"].drain()
        except KeyboardInterrupt:
            pass
        finally:
            v["mic"].stop()
            self._publish("offline")
        print("\nGoodbye.")
