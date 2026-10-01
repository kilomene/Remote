#!/usr/bin/env python3
"""remote-host: the controlled side of Remote.

Listens on TCP (default 47800), authenticates the viewer with a
challenge-response password handshake, then streams JPEG screen frames
and injects the viewer's mouse/keyboard input.

X11 is the v1 capture target (mss). Wayland capture is not yet supported.
"""
import argparse
import base64
import hashlib
import json
import logging
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "common"))

import remote_proto as proto

LOG = logging.getLogger("remote-host")

DEFAULT_CONFIG = "/etc/remote/host.conf"
FRAME_FPS = 12
JPEG_QUALITY = 60


def ensure_vendor():
    """Make vendored pure-python wheels (mss, pynput) importable if the
    system does not provide them."""
    try:
        import mss  # noqa: F401
        return
    except ImportError:
        pass
    for d in (os.environ.get("REMOTE_VENDOR_DIR"), "/opt/remote/vendor"):
        if d and os.path.isdir(d):
            sys.path.insert(0, d)
            return


def load_config(path):
    try:
        with open(path) as f:
            cfg = json.load(f)
        return (base64.b64decode(cfg["salt"]), base64.b64decode(cfg["key"]))
    except (OSError, KeyError, ValueError) as exc:
        raise SystemExit("cannot load %s: %s (run remote-set-password first)" % (path, exc))


# ---------------------------------------------------------------- capture

class ScreenCapture:
    """mss-based X11 capture. Raises RuntimeError if unavailable."""

    def __init__(self):
        ensure_vendor()
        try:
            import mss as mss_mod
        except ImportError as exc:
            raise RuntimeError("mss not available: %s" % exc)
        self._mss = mss_mod.mss()
        self._mon = self._mss.monitors[1]
        LOG.info("capturing monitor %sx%s", self._mon["width"], self._mon["height"])

    @property
    def size(self):
        return self._mon["width"], self._mon["height"]

    def grab(self):
        shot = self._mss.grab(self._mon)
        # shot.rgb is raw RGB bytes
        from PIL import Image
        return Image.frombytes("RGB", shot.size, shot.rgb)


class TestPatternCapture:
    """Synthetic frames for --self-test (no X server needed)."""

    def __init__(self, width=320, height=240):
        from PIL import Image, ImageDraw
        self._Image, self._ImageDraw = Image, ImageDraw
        self.width, self.height = width, height
        self.n = 0

    @property
    def size(self):
        return self.width, self.height

    def grab(self):
        from PIL import Image, ImageDraw
        img = Image.new("RGB", (self.width, self.height), (20, 20, 30))
        d = ImageDraw.Draw(img)
        # color bars
        bars = [(200, 40, 40), (40, 200, 40), (40, 40, 200), (200, 200, 40)]
        bw = self.width // 4
        for i, col in enumerate(bars):
            d.rectangle([i * bw, 0, (i + 1) * bw, self.height // 3], fill=col)
        # moving block encodes the frame counter
        x = (self.n * 17) % (self.width - 40)
        d.rectangle([x, self.height // 2, x + 40, self.height // 2 + 40], fill=(240, 240, 240))
        # frame number as brightness patch (machine-readable-ish)
        v = self.n % 256
        d.rectangle([0, self.height - 16, 16, self.height], fill=(v, v, v))
        self.n += 1
        return img


def encode_jpeg(img, quality=JPEG_QUALITY):
    import io
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


# ---------------------------------------------------------------- input

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


class InputInjector:
    """pynput-based injection. Raises RuntimeError if unavailable."""

    def __init__(self):
        ensure_vendor()
        from pynput.mouse import Controller as MouseController, Button
        from pynput.keyboard import Controller as KeyboardController, Key
        self._mouse = MouseController()
        self._kbd = KeyboardController()
        self._Button = Button
        self._Key = Key
        LOG.info("input injection ready (pynput)")

    def _resolve_key(self, name):
        if len(name) == 1:
            return name
        attr = _KEYMAP.get(name, name.lower())
        return getattr(self._Key, attr, None)

    def handle(self, ev):
        t = ev.get("t")
        if t == "move":
            self._mouse.position = (int(ev["x"]), int(ev["y"]))
        elif t == "click":
            btn = {"left": self._Button.left, "right": self._Button.right,
                   "middle": self._Button.middle}[ev.get("button", "left")]
            if ev.get("down", True):
                self._mouse.press(btn)
            else:
                self._mouse.release(btn)
        elif t == "scroll":
            self._mouse.scroll(int(ev.get("dx", 0)), int(ev.get("dy", 0)))
        elif t == "key":
            key = self._resolve_key(ev.get("key", ""))
            if key is None:
                LOG.warning("unknown key %r", ev.get("key"))
                return
            if ev.get("down", True):
                self._kbd.press(key)
            else:
                self._kbd.release(key)
        else:
            LOG.warning("unknown input event %r", t)


class TestInputSink:
    """Accepts (and logs) input events without injecting -- for --self-test."""

    def __init__(self):
        self.events = []

    def handle(self, ev):
        self.events.append(ev)
        LOG.info("self-test input accepted: %s", json.dumps(ev, sort_keys=True))


# ---------------------------------------------------------------- server

class HostServer:
    def __init__(self, port, bind, config_path, self_test=False, test_password=None):
        self.port = port
        self.bind = bind
        self.self_test = self_test
        if self_test:
            salt = b"selftest-salt-16"  # exactly 16 bytes
            if test_password is None:
                raise SystemExit("--self-test needs --test-password or REMOTE_TEST_PASSWORD")
            self.key = proto.derive_key(test_password, salt)
            self.salt = salt
            self.capture = TestPatternCapture()
            self.injector = TestInputSink()
        else:
            self.salt, self.key = load_config(config_path)
            try:
                self.capture = ScreenCapture()
            except RuntimeError as exc:
                raise SystemExit("screen capture unavailable: %s" % exc)
            try:
                self.injector = InputInjector()
            except Exception as exc:  # noqa: BLE001 - keep serving screen w/o input
                LOG.warning("input injection unavailable (%s); screen-only mode", exc)
                self.injector = TestInputSink()

    def serve_forever(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.bind, self.port))
        srv.listen(1)
        LOG.info("listening on %s:%d", self.bind or "0.0.0.0", self.port)
        while True:
            conn, addr = srv.accept()
            LOG.info("connection from %s", addr[0])
            try:
                self.handle_client(conn)
            except Exception as exc:  # noqa: BLE001 - one bad client must not kill us
                LOG.warning("client error: %s", exc)
            finally:
                try:
                    conn.close()
                except OSError:
                    pass
            LOG.info("client disconnected")

    def handle_client(self, conn):
        try:
            proto.server_handshake(conn, self.key, self.salt)
        except proto.AuthError as exc:
            LOG.warning("auth failed: %s", exc)
            try:
                proto.send_msg(conn, proto.AUTH_FAIL, str(exc).encode())
            except OSError:
                pass
            return
        LOG.info("client authenticated")
        stop = threading.Event()
        sender = threading.Thread(target=self._frame_loop, args=(conn, stop), daemon=True)
        sender.start()
        try:
            self._input_loop(conn, stop)
        finally:
            stop.set()
            sender.join(timeout=5)

    def _frame_loop(self, conn, stop):
        interval = 1.0 / FRAME_FPS
        last_hash = None
        try:
            while not stop.is_set():
                t0 = time.monotonic()
                try:
                    frame = encode_jpeg(self.capture.grab())
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("capture failed: %s", exc)
                    time.sleep(1)
                    continue
                digest = hashlib.sha256(frame).digest()
                if digest != last_hash:
                    try:
                        proto.send_msg(conn, proto.FRAME, frame)
                    except OSError:
                        break
                    last_hash = digest
                wait = interval - (time.monotonic() - t0)
                if wait > 0:
                    stop.wait(wait)
        finally:
            stop.set()

    def _input_loop(self, conn, stop):
        conn.settimeout(30)
        while not stop.is_set():
            try:
                mtype, payload = proto.recv_msg(conn)
            except proto.ProtocolError:
                break
            if mtype == proto.INPUT:
                try:
                    ev = json.loads(payload.decode("utf-8"))
                    self.injector.handle(ev)
                except (ValueError, KeyError, TypeError) as exc:
                    LOG.warning("bad input event: %s", exc)
            elif mtype == proto.PING:
                try:
                    proto.send_msg(conn, proto.PONG, payload)
                except OSError:
                    break
            elif mtype == proto.DISCONNECT:
                break
            else:
                LOG.warning("unexpected msg 0x%02x", mtype)


def main():
    ap = argparse.ArgumentParser(description="remote-host: Remote controlled side")
    ap.add_argument("--port", type=int, default=proto.PORT)
    ap.add_argument("--bind", default="0.0.0.0", help="interface to bind (default all)")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--self-test", action="store_true",
                    help="synthetic frames, no X server, test password auth")
    ap.add_argument("--test-password", default=os.environ.get("REMOTE_TEST_PASSWORD"))
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    server = HostServer(args.port, args.bind, args.config,
                        self_test=args.self_test, test_password=args.test_password)
    server.serve_forever()


if __name__ == "__main__":
    main()
