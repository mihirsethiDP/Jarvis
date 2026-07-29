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
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

_STATIC = Path(__file__).resolve().parent / "static" / "index.html"


class StateServer:
    def __init__(self, port: int = 8763):
        # Hard-coded loopback — the page shows live conversation content, so a
        # LAN-reachable bind is never acceptable regardless of config.
        self.host = "127.0.0.1"
        self.port = port
        self._state = {"state": "starting", "detail": ""}
        # Rolling record of what Jarvis actually did, so the employee can
        # watch each step rather than trusting a summary after the fact.
        self._activity: deque[dict] = deque(maxlen=200)
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

    # -- activity feed ----------------------------------------------------
    _VERB = {
        "tool_call": "used", "permission": "asked permission for",
        "confirmation": "asked you to confirm", "turn": "handled",
        "startup": "started", "error": "hit an error in", "setup": "setup",
        "memory": "memory",
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
