"""Local status page: a small FastAPI server broadcasting assistant state.

Binds to 127.0.0.1 only — this is a private, per-machine status view (the
orb), never a LAN service. Assistant threads publish state changes; every
connected browser gets them over a WebSocket.
"""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

_STATIC = Path(__file__).resolve().parent / "static" / "index.html"


class StateServer:
    def __init__(self, port: int = 8763, on_quit=None,
                 on_ask=None, on_listen=None, on_answer=None):
        # Hard-coded loopback — the page shows live conversation content, so a
        # LAN-reachable bind is never acceptable regardless of config.
        self.host = "127.0.0.1"
        self.port = port
        self.on_quit = on_quit
        # Typed requests, push-to-talk, and answers to a confirmation asked
        # on the page. All same-origin gated, like /quit.
        self.on_ask = on_ask
        self.on_listen = on_listen
        self.on_answer = on_answer
        self._state = {"state": "starting", "detail": ""}
        # Rolling record of what Jarvis actually did, so the employee can
        # watch each step rather than trusting a summary after the fact.
        self._activity: deque[dict] = deque(maxlen=200)
        # The typed conversation, replayed to a page opened mid-session.
        self._transcript: deque[dict] = deque(maxlen=60)
        self._pending_prompt = ""
        self._clients: list[WebSocket] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._app = self._build_app()

    def _build_app(self) -> FastAPI:
        app = FastAPI(title="Jarvis status")

        @app.on_event("startup")
        async def _capture_loop() -> None:
            self._loop = asyncio.get_running_loop()

        @app.get("/")
        async def index() -> HTMLResponse:
            if not _STATIC.exists():
                return HTMLResponse(
                    "Jarvis UI assets missing — reinstall the package.", status_code=500
                )
            return HTMLResponse(_STATIC.read_text(encoding="utf-8"))

        @app.post("/quit")
        async def quit_(request: Request) -> JSONResponse:
            # Same-origin only. Loopback is reachable from any page the
            # employee happens to have open, so without this check a random
            # website could POST here and stop the assistant.
            origin = request.headers.get("origin")
            allowed = {f"http://{h}:{self.port}" for h in ("127.0.0.1", "localhost")}
            if origin not in allowed:
                return JSONResponse({"error": "forbidden"}, status_code=403)
            if self.on_quit is not None:
                self.on_quit()
            return JSONResponse({"ok": True})

        def _same_origin(request: Request) -> bool:
            allowed = {f"http://{h}:{self.port}" for h in ("127.0.0.1", "localhost")}
            return request.headers.get("origin") in allowed

        @app.post("/ask")
        async def ask(request: Request) -> JSONResponse:
            """A typed request. Answered on the page, not spoken."""
            if not _same_origin(request):
                return JSONResponse({"error": "forbidden"}, status_code=403)
            payload = await request.json()
            text = str(payload.get("text", "")).strip()
            if not text:
                return JSONResponse({"error": "empty"}, status_code=400)
            if len(text) > 4000:
                return JSONResponse({"error": "too long"}, status_code=400)
            if self.on_ask is None:
                return JSONResponse({"error": "unavailable"}, status_code=503)
            accepted = bool(self.on_ask(text))
            if not accepted:
                return JSONResponse({"error": "busy"}, status_code=409)
            return JSONResponse({"ok": True})

        @app.post("/listen")
        async def listen(request: Request) -> JSONResponse:
            """Push-to-talk: record one utterance without the wake word."""
            if not _same_origin(request):
                return JSONResponse({"error": "forbidden"}, status_code=403)
            if self.on_listen is None:
                return JSONResponse({"error": "voice unavailable"}, status_code=503)
            if not self.on_listen():
                return JSONResponse({"error": "busy"}, status_code=409)
            return JSONResponse({"ok": True})

        @app.post("/answer")
        async def answer(request: Request) -> JSONResponse:
            """Reply to a permission or confirmation asked on the page."""
            if not _same_origin(request):
                return JSONResponse({"error": "forbidden"}, status_code=403)
            payload = await request.json()
            if self.on_answer is None:
                return JSONResponse({"error": "unavailable"}, status_code=503)
            self.on_answer(str(payload.get("answer", "")))
            return JSONResponse({"ok": True})

        @app.websocket("/ws")
        async def ws(websocket: WebSocket) -> None:
            # Browsers always send Origin; reject anything that isn't our own
            # loopback page so a malicious website can't read live state.
            origin = websocket.headers.get("origin")
            allowed = {f"http://{h}:{self.port}" for h in ("127.0.0.1", "localhost")}
            if origin is not None and origin not in allowed:
                await websocket.close(code=1008)
                return
            await websocket.accept()
            self._clients.append(websocket)
            try:
                # Replay history so a page opened mid-task isn't blank.
                await websocket.send_json({
                    "type": "snapshot",
                    "state": self._state,
                    "activity": list(self._activity),
                    "transcript": list(self._transcript),
                    "prompt": self._pending_prompt,
                })
                while True:
                    await websocket.receive_text()  # keepalive; content ignored
            except WebSocketDisconnect:
                pass
            finally:
                if websocket in self._clients:
                    self._clients.remove(websocket)

        return app

    async def _broadcast(self, payload: dict) -> None:
        dead = []
        for client in list(self._clients):
            try:
                await client.send_json(payload)
            except Exception:
                dead.append(client)
        for client in dead:
            if client in self._clients:
                self._clients.remove(client)

    def _send(self, payload: dict) -> None:
        if self._loop is not None and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._broadcast(payload), self._loop)

    def publish(self, state: str, detail: str = "") -> None:
        """Thread-safe state update from the assistant."""
        self._state = {"state": state, "detail": detail[:300]}
        self._send({"type": "state", **self._state})

    # -- conversation on the page ----------------------------------------
    def publish_message(self, role: str, text: str) -> None:
        """One line of the typed conversation ("you" or "jarvis")."""
        entry = {"type": "message", "role": role, "text": text[:4000]}
        self._transcript.append({k: v for k, v in entry.items() if k != "type"})
        self._send(entry)

    def publish_prompt(self, prompt: str) -> None:
        """Jarvis is waiting for a yes/no on the page."""
        self._pending_prompt = prompt[:1000]
        self._send({"type": "prompt", "prompt": self._pending_prompt})

    def clear_prompt(self) -> None:
        self._pending_prompt = ""
        self._send({"type": "prompt", "prompt": ""})

    # -- activity feed ----------------------------------------------------
    _VERB = {
        "tool_call": "used", "permission": "asked permission for",
        "confirmation": "asked you to confirm", "turn": "handled",
        "startup": "started", "error": "hit an error in", "setup": "setup",
        "memory": "memory", "voice": "switched to",
    }

    def record_activity(self, entry: dict) -> None:
        """Render one audit entry into the live feed. Wired to AuditLog, so
        every gated action shows up here without per-tool instrumentation."""
        item = {
            "ts": str(entry.get("ts", ""))[11:19],
            "event": entry.get("event", ""),
            "verb": self._VERB.get(entry.get("event", ""), entry.get("event", "")),
            "tool": entry.get("tool", ""),
            "detail": str(entry.get("detail", ""))[:180],
            "decision": entry.get("decision", ""),
            "ok": bool(entry.get("ok", True)),
        }
        self._activity.append(item)
        self._send({"type": "activity", **item})

    def start(self) -> None:
        config = uvicorn.Config(
            self._app, host=self.host, port=self.port, log_level="warning"
        )
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True, name="jarvis-ui")
        thread.start()
        print(f"Status page: http://{self.host}:{self.port}")
