"""Local status page: a small FastAPI server broadcasting assistant state.

Binds to 127.0.0.1 only — this is a private, per-machine status view (the
orb), never a LAN service. Assistant threads publish state changes; every
connected browser gets them over a WebSocket.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

_STATIC = Path(__file__).resolve().parent / "static" / "index.html"


class StateServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 8763):
        self.host = host
        self.port = port
        self._state = {"state": "starting", "detail": ""}
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
            return HTMLResponse(_STATIC.read_text(encoding="utf-8"))

        @app.websocket("/ws")
        async def ws(websocket: WebSocket) -> None:
            await websocket.accept()
            self._clients.append(websocket)
            try:
                await websocket.send_json(self._state)
                while True:
                    await websocket.receive_text()  # keepalive; content ignored
            except WebSocketDisconnect:
                pass
            finally:
                if websocket in self._clients:
                    self._clients.remove(websocket)

        return app

    async def _broadcast(self) -> None:
        dead = []
        for client in list(self._clients):
            try:
                await client.send_json(self._state)
            except Exception:
                dead.append(client)
        for client in dead:
            if client in self._clients:
                self._clients.remove(client)

    def publish(self, state: str, detail: str = "") -> None:
        """Thread-safe state update from the assistant."""
        self._state = {"state": state, "detail": detail[:300]}
        if self._loop is not None and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._broadcast(), self._loop)

    def start(self) -> None:
        config = uvicorn.Config(
            self._app, host=self.host, port=self.port, log_level="warning"
        )
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True, name="jarvis-ui")
        thread.start()
        print(f"Status page: http://{self.host}:{self.port}")
