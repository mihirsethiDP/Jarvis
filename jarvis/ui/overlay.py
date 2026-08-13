"""A small always-on-top status pill on the desktop.

Jarvis is meant to sit in the background all day. The browser HUD is good
when you are looking at it, but it is a whole window — nobody keeps one open
across a working day, so for most of that day the assistant is invisible and
you cannot tell whether it is listening, working, or dead.

This is the always-there piece: a compact borderless strip that shows the
current state, what Jarvis is doing right now, and an animated loader while
it works. Click it to open the full HUD; the Talk button records without the
wake word.

tkinter is used deliberately — it is in the standard library, so the
always-visible part of the product adds no dependency and nothing extra to
package. It runs on its own thread with its own event loop; every Tk call
stays on that thread, and the assistant pushes updates through a queue
rather than touching widgets from outside.
"""

from __future__ import annotations

import queue
import threading

_STATE_STYLE = {
    #  label            dot colour   show the loader
    "starting":     ("Starting…",    "#8a94a6", True),
    "idle":         ("Say “Hey Jarvis”", "#38bdf8", False),
    "listening":    ("Listening…",   "#22d3ee", False),
    "transcribing": ("Heard you — writing it down", "#a78bfa", True),
    "thinking":     ("Working…",     "#fbbf24", True),
    "working":      ("Working…",     "#fbbf24", True),
    "speaking":     ("Speaking",     "#34d399", False),
    "offline":      ("Offline",      "#ef4444", False),
}

_WIDTH, _HEIGHT = 330, 68
_MARGIN = 24


class Overlay:
    """Runs a Tk window on its own thread; updates arrive through a queue."""

    def __init__(self, port: int = 8763, on_listen=None, wake_phrase: str = "Hey Jarvis"):
        # The idle label must follow the configured wake word — telling the
        # user to say "Hey Jarvis" while Jarvis listens for "Alexa" is a trap.
        _STATE_STYLE["idle"] = (f"Say “{wake_phrase}”", "#38bdf8", False)
        self._q: queue.Queue = queue.Queue()
        self._port = port
        self._on_listen = on_listen
        self._thread: threading.Thread | None = None
        self._root = None
        self._alive = threading.Event()
        self._phase = 0

    # -- called from the assistant's threads ------------------------------
    def publish(self, state: str, detail: str = "") -> None:
        self._q.put(("state", state, detail))

    def close(self, timeout: float = 3.0) -> None:
        """Ask the window to close, and wait for its thread to finish.

        The wait matters: if the interpreter starts finalizing while the Tk
        interpreter is still alive on another thread, Tcl aborts with
        "async handler deleted by the wrong thread".
        """
        self._q.put(("close", "", ""))
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)

    def start(self) -> bool:
        try:
            import tkinter  # noqa: F401
        except ImportError:
            return False
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="jarvis-overlay")
        self._thread.start()
        # Wait briefly for the window; a failure here must not stop Jarvis.
        return self._alive.wait(5.0)

    # -- everything below runs on the overlay thread ----------------------
    def _run(self) -> None:
        try:
            self._build()
        except Exception:
            return
        self._alive.set()
        try:
            self._root.mainloop()
        except Exception:
            pass
        # Tear down on the thread that built it. Destroying elsewhere — or
        # letting the interpreter finalize it from the main thread — produces
        # "Tcl_AsyncDelete: async handler deleted by the wrong thread".
        try:
            self._root.destroy()
        except Exception:
            pass
        self._root = None
        # tkinter keeps a module-level reference to the last root it created.
        # Left set, that object is finalized on the *main* thread at
        # interpreter shutdown, which is what triggers the Tcl abort message.
        try:
            import tkinter

            tkinter._default_root = None
        except Exception:
            pass

    def _build(self) -> None:
        import tkinter as tk

        self._root = tk.Tk()
        self._root.overrideredirect(True)          # no title bar
        self._root.attributes("-topmost", True)
        self._root.configure(bg="#05070d")
        try:
            self._root.attributes("-alpha", 0.94)
        except Exception:
            pass

        screen_w = self._root.winfo_screenwidth()
        screen_h = self._root.winfo_screenheight()
        x = screen_w - _WIDTH - _MARGIN
        y = screen_h - _HEIGHT - _MARGIN - 48     # clear of the taskbar
        self._root.geometry(f"{_WIDTH}x{_HEIGHT}+{x}+{y}")

        frame = tk.Frame(self._root, bg="#05070d", highlightthickness=1,
                         highlightbackground="#1b2540")
        frame.pack(fill="both", expand=True)

        self._dot = tk.Canvas(frame, width=14, height=14, bg="#05070d",
                              highlightthickness=0)
        self._dot.place(x=12, y=12)
        self._dot_id = self._dot.create_oval(2, 2, 12, 12, fill="#38bdf8", outline="")

        self._label = tk.Label(frame, text="Starting…", bg="#05070d", fg="#d3dcef",
                               font=("Segoe UI", 10, "bold"), anchor="w")
        self._label.place(x=34, y=9, width=200)

        self._detail = tk.Label(frame, text="", bg="#05070d", fg="#78859c",
                                font=("Segoe UI", 8), anchor="w", justify="left")
        self._detail.place(x=34, y=30, width=_WIDTH - 46)

        talk = tk.Label(frame, text="Talk", bg="#0c1526", fg="#9fb0c9",
                        font=("Segoe UI", 8), padx=8, pady=3, cursor="hand2")
        talk.place(x=_WIDTH - 52, y=9)
        talk.bind("<Button-1>", lambda _e: self._talk())

        # Click the body to open the full HUD; drag to move the pill.
        for widget in (frame, self._label, self._detail):
            widget.bind("<Button-1>", self._press)
            widget.bind("<B1-Motion>", self._drag)
            widget.bind("<ButtonRelease-1>", self._release)

        self._root.after(80, self._pump)

    def _press(self, event) -> None:
        self._drag_from = (event.x_root, event.y_root)
        self._moved = False

    def _drag(self, event) -> None:
        if not getattr(self, "_drag_from", None):
            return
        dx = event.x_root - self._drag_from[0]
        dy = event.y_root - self._drag_from[1]
        if abs(dx) > 3 or abs(dy) > 3:
            self._moved = True
            self._root.geometry(
                f"+{self._root.winfo_x() + dx}+{self._root.winfo_y() + dy}")
            self._drag_from = (event.x_root, event.y_root)

    def _release(self, _event) -> None:
        # A click that did not move the window opens the full HUD.
        if not getattr(self, "_moved", False):
            import webbrowser

            webbrowser.open(f"http://127.0.0.1:{self._port}")
        self._drag_from = None

    def _talk(self) -> None:
        if self._on_listen:
            try:
                self._on_listen()
            except Exception:
                pass

    def _pump(self) -> None:
        """Drain queued updates and animate. The only place widgets change."""
        try:
            while True:
                kind, state, detail = self._q.get_nowait()
                if kind == "close":
                    self._root.quit()      # _run destroys it, on this thread
                    return
                self._apply(state, detail)
        except queue.Empty:
            pass
        except Exception:
            pass

        if getattr(self, "_loading", False):
            self._phase = (self._phase + 1) % 4
            self._label.config(text=self._base_label + "." * self._phase)
        self._root.after(220, self._pump)

    def _apply(self, state: str, detail: str) -> None:
        label, colour, loading = _STATE_STYLE.get(
            state, ("Working…", "#fbbf24", True))
        self._base_label = label.rstrip("…")
        self._loading = loading
        self._label.config(text=label, fg="#d3dcef")
        self._dot.itemconfig(self._dot_id, fill=colour)
        text = " ".join((detail or "").split())
        if len(text) > 78:
            text = text[:77] + "…"
        self._detail.config(text=text)
