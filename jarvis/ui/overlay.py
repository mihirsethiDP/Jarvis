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
_BAR_HEIGHT = 64           # collapsed: a slimmer pill
_PANEL_HEIGHT = 190        # extra height when expanded
_MARGIN = 24
_COLLAPSE_AFTER_MS = 12_000   # auto-fold this long after the content settles

_BG = "#0e1013"
_EDGE = "#3a2415"
_INK = "#e8e6e1"
_INK_DIM = "#8b8f98"
_EMBER = "#ff6b1a"
_EMBER_HI = "#ffb066"


def _enable_dpi_awareness() -> None:
    """Render at the screen's real resolution.

    Without this, Windows draws the widget at 96 DPI and stretches the bitmap
    on a 125-150% scaled laptop screen, which is why text and edges looked
    soft. Must run before the first window in the process exists; harmless if
    it was already set or the API is missing (older Windows, other OSes).
    """
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # per-monitor aware
    except Exception:
        try:
            import ctypes

            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _clip_to_rounded_rect(root, width: int, height: int, radius: int) -> None:
    """Cut the window itself to a rounded shape (Windows only).

    A Tk window is always a rectangle; drawing a pill inside it would leave
    dark corners showing. SetWindowRgn makes the area outside the shape
    genuinely not part of the window, so the desktop shows through and clicks
    there go to whatever is underneath.
    """
    try:
        import ctypes

        user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
        hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
        region = gdi32.CreateRoundRectRgn(0, 0, width + 1, height + 1,
                                          radius * 2, radius * 2)
        # The system owns the region after a successful call; don't free it.
        if not user32.SetWindowRgn(hwnd, region, True):
            gdi32.DeleteObject(region)
    except Exception:
        pass      # not Windows: the widget stays a plain rectangle


def _rounded_rect(canvas, x1, y1, x2, y2, r, **kw):
    """A rounded rectangle on a Tk canvas (smoothed polygon). r >= half the
    height gives a pill."""
    r = max(0, min(r, (x2 - x1) // 2, (y2 - y1) // 2))
    points = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
              x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
              x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return canvas.create_polygon(points, smooth=True, **kw)


class _Chip:
    """A small pill-shaped button drawn on its own canvas."""

    def __init__(self, parent, text, px, *, fill, fg, command, bold=False):
        import tkinter as tk
        import tkinter.font as tkfont

        font = tkfont.Font(family="Segoe UI", size=9,
                           weight="bold" if bold else "normal")
        h = px(24)
        w = font.measure(text) + px(26)
        bg = parent.cget("bg")
        self.widget = tk.Canvas(parent, width=w, height=h, bg=bg,
                                highlightthickness=0, bd=0, cursor="hand2")
        # A true pill: two end circles and a bar between them. (The smoothed
        # polygon only rounds this small a shape partway.)
        c = self.widget
        c.create_oval(0, 0, h - 1, h - 1, fill=fill, outline="")
        c.create_oval(w - h, 0, w - 1, h - 1, fill=fill, outline="")
        c.create_rectangle(h // 2, 0, w - h // 2, h - 1, fill=fill, outline="")
        self.widget.create_text(w // 2, h // 2, text=text, fill=fg, font=font)
        self.widget.bind("<Button-1>", lambda _e: command())

    def pack(self, **kw):
        self.widget.pack(**kw)
        return self

    def place(self, **kw):
        self.widget.place(**kw)
        return self


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

        _enable_dpi_awareness()     # before the first window: crisp, not upscaled

        self._expanded = False
        self._pinned = False          # user opened it by hand: stay open
        self._prompt_text = ""
        self._content_text = ""
        self._collapse_job = None

        self._root = tk.Tk()
        self._root.overrideredirect(True)          # no title bar
        self._root.attributes("-topmost", True)
        self._root.configure(bg=_BG)
        # Fully opaque: any transparency lets bright windows behind it bleed
        # through the text.

        # All layout below is in "design pixels" at 96 DPI, scaled to the real
        # screen. Fonts are given in points, which Tk already scales.
        self._s = max(1.0, self._root.winfo_fpixels("1i") / 96.0)
        self._w = self._px(_WIDTH)
        self._bar_h = self._px(_BAR_HEIGHT)
        self._panel_h = self._px(_PANEL_HEIGHT)

        screen_w = self._root.winfo_screenwidth()
        screen_h = self._root.winfo_screenheight()
        # Anchored by its BOTTOM edge so expansion grows upward and the bar
        # never walks into the taskbar.
        self._anchor_x = screen_w - self._w - self._px(_MARGIN)
        self._anchor_bottom = screen_h - self._px(_MARGIN + 48)

        # One canvas is the whole widget: the pill shape, its ember hairline,
        # the status dot and the Talk chip are drawn on it; the text labels
        # sit on top in the flat interior, away from the curved ends.
        self._canvas = tk.Canvas(self._root, bg=_BG, highlightthickness=0, bd=0)
        self._canvas.place(x=0, y=0, relwidth=1, relheight=1)

        # --- the expandable content panel (placed only while expanded) ---
        self._panel = tk.Frame(self._canvas, bg=_BG)
        self._content = tk.Label(
            self._panel, text="", bg=_BG, fg=_INK, font=("Segoe UI", 9),
            anchor="nw", justify="left", wraplength=self._w - self._px(56))
        self._content.pack(side="top", fill="x", padx=self._px(6),
                           pady=(self._px(4), self._px(10)))
        self._buttons = tk.Frame(self._panel, bg=_BG)
        _Chip(self._buttons, "Yes", self._px, fill=_EMBER, fg="#1a0d02",
              bold=True, command=lambda: self._answer("yes")).pack(
                  side="left", padx=(self._px(6), self._px(8)))
        _Chip(self._buttons, "No", self._px, fill="#262a31", fg=_INK_DIM,
              command=lambda: self._answer("no")).pack(side="left")
        # (self._buttons is packed only while a question is pending)

        # --- the always-visible bar, anchored to the widget's bottom edge ---
        inset = self._px(22)                  # clear of the round left end
        self._label = tk.Label(self._canvas, text="Starting…", bg=_BG, fg=_INK,
                               font=("Segoe UI", 10, "bold"), anchor="w")
        self._label.place(x=inset + self._px(26), rely=1.0,
                          y=-self._bar_h + self._px(11), width=self._px(170))
        self._detail = tk.Label(self._canvas, text="", bg=_BG, fg=_INK_DIM,
                                font=("Segoe UI", 8), anchor="w", justify="left")
        self._detail.place(x=inset + self._px(26), rely=1.0,
                           y=-self._bar_h + self._px(33),
                           width=self._w - inset * 2 - self._px(26))

        right = self._w - self._px(26)        # clear of the round right end
        self._chevron = tk.Label(self._canvas, text="▴", bg=_BG, fg=_INK_DIM,
                                 font=("Segoe UI", 10), cursor="hand2")
        self._chevron.place(x=right - self._px(12), rely=1.0,
                            y=-self._bar_h + self._px(9))
        self._chevron.bind("<Button-1>", lambda _e: self._toggle(manual=True))
        # Open the full HUD — a deliberate small target, since the body
        # click belongs to expanding the widget itself.
        hud = tk.Label(self._canvas, text="⤢", bg=_BG, fg=_INK_DIM,
                       font=("Segoe UI", 10), cursor="hand2")
        hud.place(x=right - self._px(34), rely=1.0, y=-self._bar_h + self._px(9))
        hud.bind("<Button-1>", lambda _e: self._open_hud())
        self._talk_chip = _Chip(self._canvas, "Talk", self._px, fill="#2a1a0d",
                                fg=_EMBER_HI, command=self._talk)
        self._talk_chip.place(x=right - self._px(92), rely=1.0,
                              y=-self._bar_h + self._px(10))

        self._dot_colour = _EMBER
        # Click the body to expand/collapse; drag to move.
        for widget in (self._canvas, self._label, self._detail, self._content):
            widget.bind("<Button-1>", self._press)
            widget.bind("<B1-Motion>", self._drag)
            widget.bind("<ButtonRelease-1>", self._release)

        self._apply_geometry()
        self._root.after(80, self._pump)

    def _px(self, design_pixels: float) -> int:
        return int(round(design_pixels * getattr(self, "_s", 1.0)))

    # -- geometry ----------------------------------------------------------
    def _fit_panel(self) -> None:
        """Size the expanded card to its content instead of a fixed height:
        a one-line question should not open a mostly empty card."""
        self._panel.update_idletasks()
        needed = self._content.winfo_reqheight() + self._px(28)
        if self._buttons.winfo_manager():
            needed += self._buttons.winfo_reqheight() + self._px(4)
        self._panel_h = max(self._px(72), min(needed, self._px(_PANEL_HEIGHT)))

    def _apply_geometry(self) -> None:
        if self._expanded:
            self._fit_panel()
        height = self._bar_h + (self._panel_h if self._expanded else 0)
        y = self._anchor_bottom - height
        self._root.geometry(f"{self._w}x{height}+{self._anchor_x}+{y}")
        self._root.update_idletasks()
        # A true pill when collapsed (radius = half the height); a softer
        # rounded card when expanded, where a full half-height radius would
        # eat the text area.
        radius = self._bar_h // 2 if not self._expanded else self._px(26)
        self._redraw(height, radius)
        _clip_to_rounded_rect(self._root, self._w, height, radius)

    def _redraw(self, height: int, radius: int) -> None:
        c = self._canvas
        c.delete("shape")
        _rounded_rect(c, 1, 1, self._w - 2, height - 2, radius, tags="shape",
                      fill=_BG, outline=_EDGE, width=max(1, self._px(1)))
        # Status dot: a soft halo ring around a solid core, on the bar row.
        cx = self._px(30)
        cy = height - self._bar_h + self._px(21)      # level with the title line
        r, halo = self._px(5), self._px(9)
        self._halo_id = c.create_oval(cx - halo, cy - halo, cx + halo, cy + halo,
                                      outline=self._dot_colour,
                                      width=max(1, self._px(1)), tags="shape")
        self._dot_id = c.create_oval(cx - r, cy - r, cx + r, cy + r,
                                     fill=self._dot_colour, outline="", tags="shape")
        c.tag_lower("shape")
        if self._expanded:
            pad = self._px(22)
            self._panel.place(x=pad, y=self._px(16), width=self._w - 2 * pad,
                              height=self._panel_h - self._px(12))
        else:
            self._panel.place_forget()

    def _set_expanded(self, expanded: bool) -> None:
        if expanded == self._expanded:
            return
        self._expanded = expanded
        if not expanded:
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
        if self._expanded:
            self._apply_geometry()
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
            # The halo breathes while it works, so "busy" reads at a glance.
            self._canvas.itemconfig(
                self._halo_id,
                outline=self._dot_colour if self._phase < 2 else _EDGE)
        elif hasattr(self, "_halo_id"):
            self._canvas.itemconfig(self._halo_id, outline=self._dot_colour)
        self._root.after(220, self._pump)

    def _apply(self, state: str, detail: str) -> None:
        label, colour, loading = _STATE_STYLE.get(
            state, ("Working…", "#ff6b1a", True))
        self._base_label = label.rstrip("…")
        self._loading = loading
        self._label.config(text=label, fg="#d3dcef")
        self._dot_colour = colour
        self._canvas.itemconfig(self._dot_id, fill=colour)
        self._canvas.itemconfig(self._halo_id, outline=colour)
        text = " ".join((detail or "").split())
        if len(text) > 78:
            text = text[:77] + "…"
        self._detail.config(text=text)

    def _apply_prompt(self, prompt: str) -> None:
        prompt = prompt.strip()
        self._prompt_text = prompt
        if prompt:
            self._content.config(text=prompt, fg=_EMBER_HI)
            self._buttons.pack(side="top", anchor="w")
            self._cancel_collapse()
            if self._expanded:
                self._apply_geometry()     # resize to fit the new content
            else:
                self._set_expanded(True)
        else:
            self._buttons.pack_forget()
            if self._content_text:
                self._content.config(text=self._content_text, fg=_INK)
            else:
                self._content.config(text="")
            if self._expanded:
                self._apply_geometry()
            self._schedule_collapse()

    def _apply_message(self, text: str) -> None:
        text = " ".join((text or "").split())
        if len(text) > 420:
            text = text[:419] + "…"
        self._content_text = text
        if not self._prompt_text:      # a pending question keeps priority
            self._content.config(text=text, fg=_INK)
            if text:
                if self._expanded:
                    self._apply_geometry()
                else:
                    self._set_expanded(True)
                self._schedule_collapse()
