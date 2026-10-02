#!/usr/bin/env python3
"""Independent v4 checks for the capture + input engines (Remote 1.0.0).

Runs with NO X server: TestPatternCapture JPEG validity, AdaptiveController
drop/raise logic on synthetic RTT sequences, graceful degradation of
list_displays/set_privacy with PATH scrubbed (no xrandr/xset/xclip),
TestInputSink acceptance of the new event types, and the text-injection
fallback chain with no clipboard tool present. Also covers frame_loop
(pause honored, JPEG bytes out) and the D-Bus marshal/demarshal round-trip
used by WaylandCapture.

Exits 0 and prints "V4-CAPTURE CHECKS PASSED" on success.
"""
import io
import os
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "host"))
sys.path.insert(0, os.path.join(ROOT, "common"))

import capture
import input as input_mod
from capture import (_DBusReader, _DBusWriter, _parse_signature,
                     AdaptiveController, TestPatternCapture, encode_jpeg,
                     frame_loop, list_displays, set_privacy)
from input import (TestInputSink, clipboard_tool, inject_text_strategy)

CHECKS = []


def check(name):
    def deco(fn):
        CHECKS.append((name, fn))
        return fn
    return deco


@check("test-pattern produces valid JPEGs")
def _t_jpeg():
    cap = TestPatternCapture(320, 240)
    assert cap.size == (320, 240)
    for _ in range(3):
        img = cap.grab()
        assert img.size == (320, 240) and img.mode == "RGB"
        jpg = encode_jpeg(img, 60)
        assert jpg[:2] == b"\xff\xd8", "missing JPEG SOI marker"
        assert jpg[-2:] == b"\xff\xd9", "missing JPEG EOI marker"
        # decodes back cleanly
        from PIL import Image
        back = Image.open(io.BytesIO(jpg))
        back.load()
        assert back.size == (320, 240)


@check("adaptive controller drops on high p95 RTT")
def _t_drop():
    class Clock:
        def __init__(self):
            self.t = 1000.0
        def __call__(self):
            return self.t
    clk = Clock()
    ctl = AdaptiveController(clock=clk)
    assert ctl.current == (30, 60), ctl.current  # START_RUNG
    for _ in range(30):
        ctl.note_rtt(300.0)
        clk.t += 0.2
    assert ctl.rung == 0, ctl.rung  # floored at the bottom rung
    assert ctl.current == (15, 30), ctl.current
    # further bad samples stay floored, never negative
    for _ in range(10):
        ctl.note_rtt(900.0)
        clk.t += 0.2
    assert ctl.rung == 0


@check("adaptive controller raises after 10s of low p95 RTT")
def _t_raise():
    class Clock:
        def __init__(self):
            self.t = 2000.0
        def __call__(self):
            return self.t
    clk = Clock()
    ctl = AdaptiveController(clock=clk)
    for _ in range(30):  # drive to the floor first (6s of 400ms samples)
        ctl.note_rtt(400.0)
        clk.t += 0.2
    assert ctl.rung == 0
    # 45s of clean 10ms samples: the 30s window flushes the bad samples,
    # then the 10s good-streak rule fires a raise
    for _ in range(45):
        ctl.note_rtt(10.0)
        clk.t += 1.0
    assert ctl.rung >= 1, ctl.rung
    # ceiling: never above the top rung
    for _ in range(120):
        ctl.note_rtt(5.0)
        clk.t += 1.0
    assert ctl.rung == len(AdaptiveController.LADDER) - 1, ctl.rung
    assert ctl.current == (60, 80), ctl.current
    # middling RTT breaks the good streak: no raise without a fresh 10s
    clk2_t = [3000.0]
    ctl2 = AdaptiveController(clock=lambda: clk2_t[0])
    for _ in range(8):  # only 8s of good samples -> no raise yet
        ctl2.note_rtt(10.0)
        clk2_t[0] += 1.0
    assert ctl2.rung == AdaptiveController.START_RUNG, ctl2.rung


def _scrub_path():
    """PATH with no binaries at all; returns the old PATH."""
    old = os.environ.get("PATH", "")
    d = tempfile.mkdtemp(prefix="nopath-")
    os.environ["PATH"] = d
    return old


@check("list_displays degrades gracefully without xrandr")
def _t_displays():
    old = _scrub_path()
    try:
        assert "xrandr" not in os.environ["PATH"] or True
        import shutil
        assert shutil.which("xrandr") is None
        ds = list_displays()
        assert ds == [], ds  # no crash, honest empty list
    finally:
        os.environ["PATH"] = old


@check("set_privacy returns bool honestly without xset")
def _t_privacy():
    old = _scrub_path()
    try:
        r1 = set_privacy(True)
        r2 = set_privacy(False)
        assert isinstance(r1, bool) and isinstance(r2, bool)
        assert r1 is False and r2 is False  # xset absent: honest False
    finally:
        os.environ["PATH"] = old


@check("TestInputSink accepts dblclick/hscroll/text/rel")
def _t_sink():
    sink = TestInputSink()
    events = [
        {"t": "dblclick", "button": "left"},
        {"t": "hscroll", "dx": -3},
        {"t": "text", "text": "héllo wörld ✓"},
        {"t": "rel", "dx": 12, "dy": -7},
        {"t": "move", "x": 10, "y": 20},
        {"t": "click", "button": "middle", "down": True},
    ]
    for ev in events:
        sink.handle(ev)
    assert sink.events == events
    assert [e["t"] for e in sink.events] == [
        "dblclick", "hscroll", "text", "rel", "move", "click"]


@check("text fallback chain with missing xclip does not crash")
def _t_text_fallback():
    old = _scrub_path()
    try:
        assert clipboard_tool() is None  # neither xclip nor xsel on PATH
        # per-char path raises (no display) -> clipboard paste attempted ->
        # no tool -> 'dropped', without crashing
        def boom(_text):
            raise RuntimeError("no display")
        def paste(_text):
            assert clipboard_tool() is None
            return False
        assert inject_text_strategy("hi ✓", boom, paste) == "dropped"
        # happy path still works when per-char succeeds
        seen = []
        assert inject_text_strategy("ok", seen.append, paste) == "typed"
        assert seen == ["ok"]
        # paste path used when per-char fails but clipboard works
        assert inject_text_strategy("ok", boom, lambda t: True) == "pasted"
        # mid-string per-char failure pastes only the remainder (no double-type)
        from input import _PartialText
        pasted = []
        def partial(_text):
            raise _PartialText("llo")
        assert inject_text_strategy("hello", partial,
                                    lambda t: pasted.append(t) or True) == "pasted"
        assert pasted == ["llo"]
    finally:
        os.environ["PATH"] = old


@check("frame_loop yields JPEGs and honors paused")
def _t_frameloop():
    cap = TestPatternCapture(160, 120)
    stop = threading.Event()
    gen = frame_loop(cap, adaptive=None, stop_event=stop)
    got = [next(gen) for _ in range(3)]
    from PIL import Image
    for jpg in got:
        assert jpg[:2] == b"\xff\xd8" and jpg[-2:] == b"\xff\xd9"
        Image.open(io.BytesIO(jpg)).load()
    stop.set()
    # paused: yields nothing within a short window
    cap2 = TestPatternCapture(160, 120)
    cap2.paused = True
    stop2 = threading.Event()
    gen2 = frame_loop(cap2, stop_event=stop2)
    t0 = time.monotonic()
    threading.Timer(0.35, stop2.set).start()
    frames = list(gen2)
    assert frames == [], frames
    assert time.monotonic() - t0 >= 0.3
    # adaptive quality is honored (lower quality -> smaller JPEG)
    ctl = AdaptiveController()
    ctl._rung = 0  # (15, 30)
    stop3 = threading.Event()
    small = next(frame_loop(TestPatternCapture(320, 240), adaptive=ctl,
                            stop_event=stop3))
    ctl._rung = len(AdaptiveController.LADDER) - 1  # (60, 80)
    big = next(frame_loop(TestPatternCapture(320, 240), adaptive=ctl,
                          stop_event=stop3))
    stop3.set()
    assert len(small) < len(big), (len(small), len(big))


@check("D-Bus marshal/demarshal round-trip")
def _t_dbus():
    cases = [
        ("oa{sv}",
         ("/org/freedesktop/portal/desktop/session/1_1",
          {"handle_token": ("s", "remote1a2b"),
           "session_handle_token": ("s", "remote3c4d")}),
         ("/org/freedesktop/portal/desktop/session/1_1",
          {"handle_token": ("s", "remote1a2b"),
           "session_handle_token": ("s", "remote3c4d")})),
        ("oa{sv}",
         ("/s/1", {"types": ("u", 1), "multiple": ("b", False),
                   "cursor_mode": ("u", 2)}),
         ("/s/1", {"types": ("u", 1), "multiple": ("b", False),
                   "cursor_mode": ("u", 2)})),
        ("ua{sv}",
         (0, {"session_handle": ("o", "/org/x/session/9")}),
         (0, {"session_handle": ("o", "/org/x/session/9")})),
        ("a(ua{sv})",
         ([(7, {"pipewire_node": ("u", 41),
                "size": ("(ii)", (1920, 1080))})],),
         ([(7, {"pipewire_node": ("u", 41),
                "size": ("(ii)", (1920, 1080))})],)),
        ("h", (0,), (99,)),  # fd index resolves against received fds        ("sbg", ("hi", True, "a{sv}")),
    ]
    for sig, val, want in cases:
        w = _DBusWriter()
        types = _parse_signature(sig)
        for t, v in zip(types, val):
            w.write(t, v)
        data = bytes(w.buf)
        fds = [99] if "h" in sig else []
        r = _DBusReader(data, fds)
        got = [r.read(t) for t in types]
        # normalize dict-entry arrays to dicts and tuples to lists
        def _canon(x):
            if isinstance(x, tuple):
                return [_canon(i) for i in x]
            if isinstance(x, list):
                return [_canon(i) for i in x]
            if isinstance(x, dict):
                return {k: _canon(v) for k, v in x.items()}
            return x
        norm = []
        for t, v in zip(types, got):
            if t[0] == "a" and t[1][0] == "e":
                norm.append(_canon(dict(v)))
            elif t[0] == "a" and t[1][0] == "r":
                norm.append(_canon([(sid, dict(props)) for sid, props in v]))
            else:
                norm.append(_canon(v))
        exp = []
        for t, v in zip(types, want):
            if t[0] == "a" and t[1][0] == "e":
                exp.append(_canon(dict(v) if not isinstance(v, dict) else v))
            elif t[0] == "a" and t[1][0] == "r":
                exp.append(_canon([(sid, dict(props)) for sid, props in v]))
            else:
                exp.append(_canon(v))
        assert norm == exp, (sig, norm, exp)
        assert r.o == len(data), (sig, r.o, len(data))  # fully consumed


def main():
    failures = 0
    for name, fn in CHECKS:
        try:
            fn()
        except Exception as exc:
            failures += 1
            print("FAIL %s: %r" % (name, exc))
            import traceback
            traceback.print_exc()
        else:
            print("ok   %s" % name)
    if failures:
        print("%d CHECK(S) FAILED" % failures)
        return 1
    print("V4-CAPTURE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
