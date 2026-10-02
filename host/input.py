#!/usr/bin/env python3
"""host/input.py -- extended input injector for Remote 1.0.0 (Linux host).

pynput-based injection (real; raises RuntimeError without pynput or
without an X display). Event schema (INPUT 0x11 JSON payload):

  {"t":"move","x":n,"y":n}            absolute pointer move (mouse perm)
  {"t":"rel","dx":n,"dy":n}           relative move, scaled by accel (mouse)
  {"t":"click","button":"left|right|middle","down":bool}              (mouse)
  {"t":"dblclick","button":"left"}    two click pairs, 60ms gap    (mouse)
  {"t":"scroll","dx":n,"dy":n}        vertical/horizontal wheel    (mouse)
  {"t":"hscroll","dx":n}              horizontal scroll            (mouse)
  {"t":"key","key":name,"down":bool}  keysym name or single char (keyboard)
  {"t":"text","text":"..."}           Unicode text                 (keyboard)

Text injection tries per-character key events first; on any failure it
falls back to a real clipboard paste (xclip/xsel + Ctrl+V), restoring the
previous clipboard best-effort. When neither path exists the text is
dropped with a warning -- never faked.

$0: stdlib only (+ pynput via the vendored wheel).
"""
import json
import logging
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

LOG = logging.getLogger("remote-input")

MIN_ACCEL, MAX_ACCEL = 0.25, 4.0
DBLCLICK_GAP_S = 0.06


def ensure_vendor():
    """Make vendored pure-python wheels (pynput) importable if the system
    does not provide them."""
    try:
        import pynput  # noqa: F401
        return
    except ImportError:
        pass
    for d in (os.environ.get("REMOTE_VENDOR_DIR"), "/opt/remote/vendor"):
        if d and os.path.isdir(d):
            sys.path.insert(0, d)
            return


_KEYMAP = {
    "Return": "enter", "BackSpace": "backspace", "Tab": "tab",
    "Escape": "esc", "space": "space",
    "Shift_L": "shift", "Shift_R": "shift_r",
    "Control_L": "ctrl", "Control_R": "ctrl_r",
    "Alt_L": "alt", "Alt_R": "alt_r",
    "Super_L": "cmd", "Super_R": "cmd_r",
    "Up": "up", "Down": "down", "Left": "left", "Right": "right",
    "Delete": "delete", "Home": "home", "End": "end",
    "Page_Up": "page_up", "Page_Down": "page_down",
    "Caps_Lock": "caps_lock",
}
for _i in range(1, 13):
    _KEYMAP["F%d" % _i] = "f%d" % _i

_BUTTONS = ("left", "right", "middle")


# ------------------------------------------------- clipboard helpers
# (module-level: real subprocess logic, unit-testable without a display)


def clipboard_tool():
    """Return 'xclip', 'xsel', or None -- whichever can drive the X clipboard."""
    if shutil.which("xclip"):
        return "xclip"
    if shutil.which("xsel"):
        return "xsel"
    return None


def clipboard_get(tool):
    """Best-effort clipboard read (bytes); None on any failure."""
    try:
        if tool == "xclip":
            r = subprocess.run(["xclip", "-selection", "clipboard", "-o"],
                               capture_output=True, timeout=5)
        elif tool == "xsel":
            r = subprocess.run(["xsel", "--clipboard", "--output"],
                               capture_output=True, timeout=5)
        else:
            return None
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def clipboard_set(tool, data):
    """Write bytes to the clipboard. Returns True on success."""
    try:
        if tool == "xclip":
            r = subprocess.run(["xclip", "-selection", "clipboard"], input=data,
                               capture_output=True, timeout=5)
        elif tool == "xsel":
            r = subprocess.run(["xsel", "--clipboard", "--input"], input=data,
                               capture_output=True, timeout=5)
        else:
            return False
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def inject_text_strategy(text, type_fn, paste_fn):
    """Decide how to inject *text*: try per-char key events (*type_fn*)
    first; on any failure fall back to clipboard paste (*paste_fn*) for the
    *remaining* (not yet typed) suffix, so a mid-string failure does not
    double-type the prefix.

    Returns 'typed', 'pasted', or 'dropped'. Pure decision logic --
    unit-testable without a display server.
    """
    try:
        type_fn(text)
    except _PartialText as exc:
        remaining = exc.remaining
        LOG.warning("per-char text injection stopped partway; pasting remainder")
    except Exception as exc:
        LOG.warning("per-char text injection failed (%r); trying clipboard paste", exc)
        remaining = text
    else:
        return "typed"
    try:
        if paste_fn(remaining):
            return "pasted"
    except Exception as exc:
        LOG.warning("clipboard paste failed (%r)", exc)
    return "dropped"


class _PartialText(Exception):
    """Raised by _type_per_char when injection fails mid-string; carries
    the not-yet-typed suffix so the clipboard fallback pastes only that."""

    def __init__(self, remaining):
        super().__init__("per-char injection failed partway")
        self.remaining = remaining


# ------------------------------------------------- injector


class InputInjector:
    """pynput-based injection. Raises RuntimeError if unavailable.

    *accel* scales relative ("rel") pointer moves; absolute moves are
    never scaled. Must be within 0.25-4.0 (ValueError otherwise).
    """

    def __init__(self, accel=1.0):
        accel = float(accel)
        if not MIN_ACCEL <= accel <= MAX_ACCEL:
            raise ValueError("accel must be within %.2f-%.2f, got %r"
                             % (MIN_ACCEL, MAX_ACCEL, accel))
        self.accel = accel
        ensure_vendor()
        try:
            from pynput.mouse import Controller as MouseController, Button
            from pynput.keyboard import Controller as KeyboardController, Key
        except ImportError as exc:
            raise RuntimeError("pynput not available: %s" % exc)
        self._mouse = MouseController()
        self._kbd = KeyboardController()
        self._Button = Button
        self._Key = Key
        LOG.info("input injection ready (pynput, accel=%.2f)", accel)

    # -- helpers ------------------------------------------------------

    def _resolve_key(self, name):
        if len(name) == 1:
            return name
        attr = _KEYMAP.get(name, name.lower())
        return getattr(self._Key, attr, None)

    def _resolve_button(self, name):
        name = (name or "left").lower()
        if name not in _BUTTONS:
            LOG.warning("unknown button %r; using left", name)
            name = "left"
        return getattr(self._Button, name)

    def _type_per_char(self, text):
        for i, ch in enumerate(text):
            try:
                self._kbd.press(ch)
                self._kbd.release(ch)
            except Exception:
                LOG.warning("per-char injection failed at char %d (%r)", i, ch)
                raise _PartialText(text[i:])

    def _paste_via_clipboard(self, text):
        """Set the clipboard to *text* and press Ctrl+V. Returns True when
        a paste was attempted; restores the previous clipboard best-effort."""
        tool = clipboard_tool()
        if tool is None:
            return False
        old = clipboard_get(tool)
        if not clipboard_set(tool, text.encode("utf-8")):
            return False
        try:
            self._kbd.press(self._Key.ctrl)
            self._kbd.press("v")
            self._kbd.release("v")
            self._kbd.release(self._Key.ctrl)
        finally:
            if old is not None:
                clipboard_set(tool, old)
        return True

    def _inject_text(self, text):
        if not text:
            return
        outcome = inject_text_strategy(text, self._type_per_char,
                                       self._paste_via_clipboard)
        if outcome == "dropped":
            LOG.warning("dropping text input: no per-char path and no "
                        "clipboard tool (xclip/xsel) available")

    # -- event dispatch ------------------------------------------------

    def handle(self, ev):
        t = ev.get("t")
        if t == "move":
            self._mouse.position = (int(ev["x"]), int(ev["y"]))
        elif t == "rel":
            dx = int(round(float(ev.get("dx", 0)) * self.accel))
            dy = int(round(float(ev.get("dy", 0)) * self.accel))
            self._mouse.move(dx, dy)
        elif t == "click":
            btn = self._resolve_button(ev.get("button"))
            if ev.get("down", True):
                self._mouse.press(btn)
            else:
                self._mouse.release(btn)
        elif t == "dblclick":
            btn = self._resolve_button(ev.get("button"))
            self._mouse.press(btn)
            self._mouse.release(btn)
            time.sleep(DBLCLICK_GAP_S)
            self._mouse.press(btn)
            self._mouse.release(btn)
        elif t == "scroll":
            self._mouse.scroll(int(ev.get("dx", 0)), int(ev.get("dy", 0)))
        elif t == "hscroll":
            self._mouse.scroll(int(ev.get("dx", 0)), 0)
        elif t == "key":
            key = self._resolve_key(ev.get("key", ""))
            if key is None:
                LOG.warning("unknown key %r", ev.get("key"))
                return
            if ev.get("down", True):
                self._kbd.press(key)
            else:
                self._kbd.release(key)
        elif t == "text":
            self._inject_text(str(ev.get("text", "")))
        else:
            LOG.warning("unknown input event %r", t)


class TestInputSink:
    """Accepts (and logs) input events without injecting -- for self-test.

    Records every event verbatim, including the v4 types
    (dblclick/hscroll/text/rel).
    """

    def __init__(self):
        self.events = []

    def handle(self, ev):
        self.events.append(ev)
        LOG.info("self-test input accepted: %s", json.dumps(ev, sort_keys=True))
