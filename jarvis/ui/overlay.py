"""An always-on-top, expandable status widget on the desktop.

DP Assistant is meant to sit in the background all day. The browser HUD is
good when you are looking at it, but it is a whole window — nobody keeps one
open across a working day, so for most of that day the assistant would be
invisible.

This is the always-there piece, in two sizes:

- Collapsed: a compact strip — state dot, one line of "what's happening",
  a Talk button. The desk-corner presence.
- Expanded: the strip grows a content panel showing what the assistant has
  to say right now — the question it is asking (with Yes / No buttons that
  actually answer it), or its latest reply. It expands ITSELF whenever there
  is something worth seeing and folds back when the moment passes; a click
  on the body toggles it manually, and a manual open stays open until the
  user folds it.

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
    "starting":     ("Starting…",    "#8b8f98", True),
    "idle":         ("Say “Hey Jarvis”", "#ff6b1a", False),
    "listening":    ("Listening…",   "#ffb066", False),
    "transcribing": ("Heard you — writing it down", "#ffb066", True),
    "thinking":     ("Working…",     "#ff6b1a", True),
    "working":      ("Working…",     "#ff6b1a", True),
    "speaking":     ("Speaking",     "#57c78a", False),
    "offline":      ("Offline",      "#ef4444", False),
}

_WIDTH = 340
_BAR_HEIGHT = 68           # collapsed
_PANEL_HEIGHT = 190        # extra height when expanded
_MARGIN = 24
_COLLAPSE_AFTER_MS = 12_000   # auto-fold this long after the content settles

_BG = "#0e1013"
_EDGE = "#3a2415"
_INK = "#e8e6e1"
_INK_DIM = "#8b8f98"
_EMBER = "#ff6b1a"
_EMBER_HI = "#ffb066"


class Overlay:
    """Runs a Tk window on its own thread; updates arrive through a queue."""

    def __init__(self, port: int = 8763, on_listen=None, on_answer=None,
                 wake_phrase: str = "Hey Jarvis"):
        # The idle label must follow the configured wake word — telling the
        # user to say "Hey Jarvis" while it listens for "Alexa" is a trap.
        _STATE_STYLE["idle"] = (f"Say “{wake_phrase}”", "#ff6b1a", False)
        self._q: queue.Queue = queue.Queue()
        self._port = port
        self._on_listen = on_listen
        # Answers a pending confirmation exactly like the HUD's buttons do —
        # the widget is enough to approve or refuse without opening anything.
        self._on_answer = on_answer
        self._thread: threading.Thread | None = None
        self._root = None
        self._alive = threading.Event()
        self._phase = 0

    # -- called from the assistant's threads ------------------------------
    def publish(self, state: str, detail: str = "") -> None:
        self._q.put(("state", state, detail))

    def publish_prompt(self, prompt: str) -> None:
        """A question awaiting the user: expand, show it, arm Yes/No."""
        self._q.put(("prompt", prompt, ""))

    def clear_prompt(self) -> None:
        self._q.put(("prompt", "", ""))

    def publish_message(self, text: str) -> None:
        """The assistant's latest reply — worth a glance, so expand."""
        self._q.put(("message", text, ""))

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
                                        name="assistant-overlay")
        self._thread.start()
        # Wait briefly for the window; a failure here must not stop startup.
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

        self._expanded = False
        self._pinned = False          # user opened it by hand: stay open
        self._prompt_text = ""
        self._content_text = ""
        self._collapse_job = None

        self._root = tk.Tk()
        self._root.overrideredirect(True)          # no title bar
        self._root.attributes("-topmost", True)
        self._root.configure(bg=_BG)
        try:
            self._root.attributes("-alpha", 0.94)
        except Exception:
            pass

        screen_w = self._root.winfo_screenwidth()
        screen_h = self._root.winfo_screenheight()
        # Anchored by its BOTTOM edge so expansion grows upward and the bar
        # never walks into the taskbar.
        self._anchor_x = screen_w - _WIDTH - _MARGIN
        self._anchor_bottom = screen_h - _MARGIN - 48
        self._apply_geometry()

        frame = tk.Frame(self._root, bg=_BG, highlightthickness=1,
                         highlightbackground=_EDGE)
        frame.pack(fill="both", expand=True)

        # --- the expandable content panel (packed/forgotten as a whole) ---
        self._panel = tk.Frame(frame, bg=_BG)

        self._content = tk.Label(
            self._panel, text="", bg=_BG, fg=_INK, font=("Segoe UI", 9),
            anchor="nw", justify="left", wraplength=_WIDTH - 40)
        self._content.pack(fill="both", expand=True, padx=14, pady=(12, 6))

        self._buttons = tk.Frame(self._panel, bg=_BG)
        yes = tk.Label(self._buttons, text="  Yes  ", bg=_EMBER, fg="#1a0d02",
                       font=("Segoe UI", 10, "bold"), pady=4, cursor="hand2")
        no = tk.Label(self._buttons, text="  No  ", bg="#22252c", fg=_INK_DIM,
                      font=("Segoe UI", 10), pady=4, cursor="hand2")
        yes.pack(side="left", padx=(14, 8))
        no.pack(side="left")
        yes.bind("<Button-1>", lambda _e: self._answer("yes"))
        no.bind("<Button-1>", lambda _e: self._answer("no"))
        # (self._buttons is packed only while a question is pending)

        # --- the always-visible bar (packed last: bottom of the window) ---
        bar = tk.Frame(frame, bg=_BG, height=_BAR_HEIGHT - 2)
        bar.pack(side="bottom", fill="x")
        bar.pack_propagate(False)

        self._dot = tk.Canvas(bar, width=14, height=14, bg=_BG,
                              highlightthickness=0)
        self._dot.place(x=12, y=12)
        self._dot_id = self._dot.create_oval(2, 2, 12, 12, fill=_EMBER, outline="")

        self._label = tk.Label(bar, text="Starting…", bg=_BG, fg=_INK,
                               font=("Segoe UI", 10, "bold"), anchor="w")
        self._label.place(x=34, y=9, width=178)

        self._detail = tk.Label(bar, text="", bg=_BG, fg=_INK_DIM,
                                font=("Segoe UI", 8), anchor="w", justify="left")
        self._detail.place(x=34, y=30, width=_WIDTH - 46)

        talk = tk.Label(bar, text="Talk", bg="#22150b", fg=_EMBER_HI,
                        font=("Segoe UI", 8), padx=8, pady=3, cursor="hand2")
        talk.place(x=_WIDTH - 92, y=9)
        talk.bind("<Button-1>", lambda _e: self._talk())

        # Open the full HUD — a deliberate small target, since the body
        # click now belongs to expanding the widget itself.
        hud = tk.Label(bar, text="⤢", bg=_BG, fg=_INK_DIM,
                       font=("Segoe UI", 10), cursor="hand2")
        hud.place(x=_WIDTH - 44, y=8)
        hud.bind("<Button-1>", lambda _e: self._open_hud())

        self._chevron = tk.Label(bar, text="▴", bg=_BG, fg=_INK_DIM,
                                 font=("Segoe UI", 10), cursor="hand2")
        self._chevron.place(x=_WIDTH - 24, y=8)
        self._chevron.bind("<Button-1>", lambda _e: self._toggle(manual=True))

        # Click the body to expand/collapse; drag to move.
        for widget in (bar, self._label, self._detail, self._content):
            widget.bind("<Button-1>", self._press)
            widget.bind("<B1-Motion>", self._drag)
            widget.bind("<ButtonRelease-1>", self._release)

        self._root.after(80, self._pump)

    # -- geometry ----------------------------------------------------------
    def _apply_geometry(self) -> None:
        height = _BAR_HEIGHT + (_PANEL_HEIGHT if self._expanded else 0)
        y = self._anchor_bottom - height
        self._root.geometry(f"{_WIDTH}x{height}+{self._anchor_x}+{y}")

    def _set_expanded(self, expanded: bool) -> None:
        if expanded == self._expanded:
            return
        self._expanded = expanded
        if expanded:
            self._panel.pack(side="top", fill="both", expand=True)
        else:
            self._panel.pack_forget()
            self._pinned = False
        self._chevron.config(text="▾" if expanded else "▴")
        self._apply_geometry()

    def _toggle(self, manual: bool = False) -> None:
        self._set_expanded(not self._expanded)
        if manual and self._expanded:
            self._pinned = True
            self._cancel_collapse()

    def _schedule_collapse(self) -> None:
        """Fold back after a quiet spell — unless a question is pending or
        the user opened the panel themselves."""
        self._cancel_collapse()
        if self._prompt_text or self._pinned:
            return
        self._collapse_job = self._root.after(
            _COLLAPSE_AFTER_MS, lambda: self._set_expanded(False))

    def _cancel_collapse(self) -> None:
        if getattr(self, "_collapse_job", None):
            try:
                self._root.after_cancel(self._collapse_job)
            except Exception:
                pass
            self._collapse_job = None

    # -- interactions --------------------------------------------------------
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
            self._anchor_x = self._root.winfo_x() + dx
            self._anchor_bottom = (self._root.winfo_y() + dy
                                   + self._root.winfo_height())
            self._apply_geometry()
            self._drag_from = (event.x_root, event.y_root)

    def _release(self, _event) -> None:
        # A click that did not move the window toggles the panel.
        if not getattr(self, "_moved", False):
            self._toggle(manual=True)
        self._drag_from = None

    def _open_hud(self) -> None:
        import webbrowser

        webbrowser.open(f"http://127.0.0.1:{self._port}")

    def _talk(self) -> None:
        if self._on_listen:
            try:
                self._on_listen()
            except Exception:
                pass

    def _answer(self, answer: str) -> None:
        if not self._prompt_text:
            return
        self._prompt_text = ""
        self._buttons.pack_forget()
        self._content.config(text="")
        self._schedule_collapse()
        if self._on_answer:
            try:
                self._on_answer(answer)
            except Exception:
                pass

    # -- queue pump ----------------------------------------------------------
    def _pump(self) -> None:
        """Drain queued updates and animate. The only place widgets change."""
        try:
            while True:
                kind, a, _b = self._q.get_nowait()
                if kind == "close":
                    self._root.quit()      # _run destroys it, on this thread
                    return
                if kind == "state":
                    self._apply(a, _b)
                elif kind == "prompt":
                    self._apply_prompt(a)
                elif kind == "message":
                    self._apply_message(a)
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
            state, ("Working…", "#ff6b1a", True))
        self._base_label = label.rstrip("…")
        self._loading = loading
        self._label.config(text=label, fg="#d3dcef")
        self._dot.itemconfig(self._dot_id, fill=colour)
        text = " ".join((detail or "").split())
        if len(text) > 78:
            text = text[:77] + "…"
        self._detail.config(text=text)

    def _apply_prompt(self, prompt: str) -> None:
        prompt = prompt.strip()
        self._prompt_text = prompt
        if prompt:
            self._content.config(text=prompt, fg=_EMBER_HI)
            self._buttons.pack(side="bottom", anchor="w", pady=(0, 12))
            self._cancel_collapse()
            self._set_expanded(True)
        else:
            self._buttons.pack_forget()
            if self._content_text:
                self._content.config(text=self._content_text, fg=_INK)
            else:
                self._content.config(text="")
            self._schedule_collapse()

    def _apply_message(self, text: str) -> None:
        text = " ".join((text or "").split())
        if len(text) > 420:
            text = text[:419] + "…"
        self._content_text = text
        if not self._prompt_text:      # a pending question keeps priority
            self._content.config(text=text, fg=_INK)
            if text:
                self._set_expanded(True)
                self._schedule_collapse()
