#!/usr/bin/env python3
"""remote-host: the controlled side of Remote.

Listens on TCP (default 47800), authenticates the viewer with a
challenge-response password handshake, then streams JPEG screen frames,
injects the viewer's mouse/keyboard input, syncs clipboards, serves
file transfers (protocol v2), and handles v3 features: whitelisted
system commands, pty-backed remote terminal, agent status, display
listing, and session chat.

X11 is the capture target (mss). Wayland capture is not yet supported.
"""
import argparse
import base64
import datetime
import hashlib
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "common"))

import remote_proto as proto
from file_transfer import FileTransfer, JailError, CHUNK as FILE_CHUNK
from sys_cmd import SysCmdExecutor
from terminal import TerminalManager, TerminalError
from agent_status import collect_status
from displays import get_displays
from chat import ChatLog

LOG = logging.getLogger("remote-host")

DEFAULT_CONFIG = "/etc/remote/host.conf"
DEFAULT_TRUSTED = "/etc/remote/trusted_devices"
DEFAULT_CONN_LOG = "/var/log/remote/connections.log"
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
        salt = base64.b64decode(cfg["salt"])
        key = base64.b64decode(cfg["key"])
        file_root = cfg.get("file_root") or os.path.expanduser("~")
        monitored = cfg.get("monitored_services") or ["remote-host", "tailscaled"]
        return salt, key, file_root, monitored
    except (OSError, KeyError, ValueError) as exc:
        raise SystemExit("cannot load %s: %s (run remote-set-password first)" % (path, exc))


# ---------------------------------------------------------------- capture
# (unchanged from v1)

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
# (unchanged from v1)

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


# ---------------------------------------------------------------- pairing

class DeviceStore:
    """Trusted devices (trust-on-first-use via password). JSON list at path.

    Each entry carries a permissions dict with the seven boolean flags from
    proto.PERMISSION_FLAGS. New devices default to all-true (preserves the
    trust-on-first-use behavior); entries written before permissions existed
    are migrated on load. An in-memory cache (write-through to disk) keeps
    per-message permission lookups cheap.
    """

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._devices = None

    def _read_file(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
            return data.get("devices", []) if isinstance(data, dict) else []
        except (OSError, ValueError):
            return []

    def _write_file(self, devices):
        tmp = self.path + ".tmp"
        try:
            d = os.path.dirname(self.path)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(tmp, "w") as f:
                json.dump({"devices": devices}, f, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError as exc:
            LOG.warning("cannot persist trusted devices: %s", exc)

    @staticmethod
    def _default_perms():
        return {f: True for f in proto.PERMISSION_FLAGS}

    def _ensure_perms(self, entry):
        """Migrate an entry to a full seven-flag boolean permissions dict."""
        perms = entry.get("permissions")
        if not isinstance(perms, dict):
            perms = {}
        for f in proto.PERMISSION_FLAGS:
            if not isinstance(perms.get(f), bool):
                perms[f] = True
        entry["permissions"] = perms

    def _load_locked(self):
        if self._devices is None:
            self._devices = self._read_file()
            for d in self._devices:
                self._ensure_perms(d)
            self._write_file(self._devices)

    def trust(self, device):
        """Record a device as trusted (idempotent). Returns True if new."""
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._lock:
            self._load_locked()
            for d in self._devices:
                if d.get("device_id") == device.get("device_id"):
                    d["last_seen"] = now
                    d["device_name"] = device.get("device_name", d.get("device_name"))
                    d["platform"] = device.get("platform", d.get("platform"))
                    self._ensure_perms(d)
                    self._write_file(self._devices)
                    return False
            self._devices.append({
                "device_id": device.get("device_id", "unknown"),
                "device_name": device.get("device_name", "unknown"),
                "platform": device.get("platform", "unknown"),
                "first_seen": now,
                "last_seen": now,
                "permissions": self._default_perms(),
            })
            self._write_file(self._devices)
            return True

    def set_permissions(self, device_id, perms):
        """Replace a device's permission flags. Returns False if unknown."""
        with self._lock:
            self._load_locked()
            for d in self._devices:
                if d.get("device_id") == device_id:
                    d["permissions"] = {f: bool(perms[f])
                                        for f in proto.PERMISSION_FLAGS}
                    self._write_file(self._devices)
                    return True
            return False

    def get_permissions(self, device_id):
        """Current permission flags for a device, or None if unknown."""
        with self._lock:
            self._load_locked()
            for d in self._devices:
                if d.get("device_id") == device_id:
                    return dict(d.get("permissions") or self._default_perms())
            return None

    def list_devices(self):
        """All trusted devices (copies) with their permissions."""
        with self._lock:
            self._load_locked()
            return [dict(d) for d in self._devices]


class ConnectionLog:
    """Append-only connection history."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()

    def log(self, event, device):
        line = "%s %s %s %s\n" % (
            datetime.datetime.now(datetime.timezone.utc).isoformat(),
            event,
            device.get("device_id", "unknown"),
            device.get("device_name", "unknown"),
        )
        with self._lock:
            try:
                d = os.path.dirname(self.path)
                if d:
                    os.makedirs(d, exist_ok=True)
                with open(self.path, "a") as f:
                    f.write(line)
            except OSError as exc:
                LOG.warning("cannot write connection log: %s", exc)


# ---------------------------------------------------------------- clipboard

class ClipboardSync:
    """X11 clipboard monitor. Pushes local changes to viewers via the
    server broadcast; applies received text locally.

    Backends: xclip, xsel, or in-memory (self-test / headless).
    """

    POLL_INTERVAL = 1.5

    def __init__(self, broadcast, memory=False):
        self._broadcast = broadcast
        self._lock = threading.Lock()
        self._last_remote = None  # text we applied from a viewer (echo guard)
        self._last_seen = None
        self._backend = "memory" if memory else self._detect()
        self._mem = ""
        if self._backend == "none":
            LOG.warning("no clipboard backend (xclip/xsel missing); clipboard sync disabled")
        else:
            LOG.info("clipboard backend: %s", self._backend)

    @staticmethod
    def _detect():
        if shutil.which("xclip"):
            return "xclip"
        if shutil.which("xsel"):
            return "xsel"
        return "none"

    @property
    def enabled(self):
        return self._backend != "none"

    def get(self):
        try:
            if self._backend == "memory":
                return self._mem
            if self._backend == "xclip":
                out = subprocess.run(["xclip", "-o", "-selection", "clipboard"],
                                     capture_output=True, timeout=5)
            elif self._backend == "xsel":
                out = subprocess.run(["xsel", "--clipboard", "--output"],
                                     capture_output=True, timeout=5)
            else:
                return None
            if out.returncode != 0:
                return None
            return out.stdout.decode("utf-8", "replace")
        except (OSError, subprocess.SubprocessError):
            return None

    def set(self, text):
        """Apply text received from a viewer (marks it to avoid echo)."""
        with self._lock:
            self._last_remote = text
        try:
            if self._backend == "memory":
                self._mem = text
            elif self._backend == "xclip":
                subprocess.run(["xclip", "-i", "-selection", "clipboard"],
                               input=text.encode("utf-8"), timeout=5, check=False)
            elif self._backend == "xsel":
                subprocess.run(["xsel", "--clipboard", "--input"],
                               input=text.encode("utf-8"), timeout=5, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            LOG.warning("clipboard set failed: %s", exc)

    def current_text(self):
        return self.get()

    def monitor_loop(self, stop):
        if not self.enabled:
            return
        while not stop.is_set():
            text = self.get()
            with self._lock:
                last_remote = self._last_remote
            # skip echoes of text we applied from a viewer, and no-change polls
            if text is not None and text != self._last_seen and text != last_remote:
                self._last_seen = text
                payload = json.dumps({"text": text}).encode("utf-8")
                self._broadcast(proto.CLIPBOARD_SET, payload)
                LOG.debug("clipboard change broadcast (%d chars)", len(text))
            stop.wait(self.POLL_INTERVAL)


# ---------------------------------------------------------------- server

class _ClientSession:
    """Per-connection state: socket, device info, upload-in-progress."""

    def __init__(self, conn, device):
        self.conn = conn
        self.device = device
        self.send_lock = threading.Lock()
        self.put_state = None  # {"path","size","received","fh"} during upload

    def send(self, mtype, payload=b""):
        with self.send_lock:
            proto.send_msg(self.conn, mtype, payload)


class HostServer:
    def __init__(self, port, bind, config_path, self_test=False, test_password=None):
        self.port = port
        self.bind = bind
        self.self_test = self_test
        self._sessions = set()
        self._sessions_lock = threading.Lock()
        if self_test:
            salt = b"selftest-salt-16"  # exactly 16 bytes
            if test_password is None:
                raise SystemExit("--self-test needs --test-password or REMOTE_TEST_PASSWORD")
            self.key = proto.derive_key(test_password, salt)
            self.salt = salt
            self.file_root = os.environ.get("REMOTE_FILE_ROOT") or "/tmp/remote-selftest-files"
            self.capture = TestPatternCapture()
            self.injector = TestInputSink()
            self.devices = DeviceStore(os.environ.get("REMOTE_TRUSTED_FILE")
                                       or "/tmp/remote-selftest-trusted")
            self.conn_log = ConnectionLog(os.environ.get("REMOTE_CONN_LOG")
                                          or "/tmp/remote-selftest-connections.log")
            self.clipboard = ClipboardSync(self.broadcast, memory=True)
            self.monitored_services = ["remote-host", "tailscaled"]
        else:
            self.salt, self.key, self.file_root, self.monitored_services = \
                load_config(config_path)
            try:
                self.capture = ScreenCapture()
            except RuntimeError as exc:
                raise SystemExit("screen capture unavailable: %s" % exc)
            try:
                self.injector = InputInjector()
            except Exception as exc:  # noqa: BLE001 - keep serving screen w/o input
                LOG.warning("input injection unavailable (%s); screen-only mode", exc)
                self.injector = TestInputSink()
            self.devices = DeviceStore(DEFAULT_TRUSTED)
            self.conn_log = ConnectionLog(DEFAULT_CONN_LOG)
            self.clipboard = ClipboardSync(self.broadcast)

        self.files = FileTransfer(self.file_root)
        LOG.info("file root: %s", self.files.root)

        # v3 services
        self.sys_cmd = SysCmdExecutor(
            dry_run=self_test,
            displays_fn=lambda: get_displays(getattr(self.capture, "size", None)),
            monitored_services=self.monitored_services)
        self.terminals = TerminalManager()
        self.chat = ChatLog()

        # Modular handler registry: one function per message type.
        # New protocol features register here without touching the loop.
        self.handlers = {
            proto.INPUT: self._h_input,
            proto.PING: self._h_ping,
            proto.DISCONNECT: self._h_disconnect,
            proto.CLIPBOARD_SET: self._h_clipboard,
            proto.FILE_LIST: self._h_file_list,
            proto.FILE_GET: self._h_file_get,
            proto.FILE_PUT: self._h_file_put,
            proto.FILE_DATA: self._h_file_data,
            proto.FILE_DONE: self._h_file_done,
            proto.FILE_MKDIR: self._h_file_mkdir,
            proto.FILE_DELETE: self._h_file_delete,
            proto.FILE_RENAME: self._h_file_rename,
            proto.SYSTEM_CMD: self._h_system_cmd,
            proto.TERMINAL_OPEN: self._h_terminal_open,
            proto.TERMINAL_DATA: self._h_terminal_data,
            proto.TERMINAL_CLOSE: self._h_terminal_close,
            proto.CHAT_MSG: self._h_chat,
            proto.AGENT_QUERY: self._h_agent_query,
            proto.DISPLAYS_QUERY: self._h_displays_query,
            proto.PERMS_SET: self._h_perms_set,
            proto.PERMS_LIST: self._h_perms_list,
        }

    # -- session bookkeeping -------------------------------------------------

    def broadcast(self, mtype, payload=b""):
        with self._sessions_lock:
            sessions = list(self._sessions)
        for s in sessions:
            try:
                s.send(mtype, payload)
            except OSError:
                pass

    def broadcast_others(self, exclude, mtype, payload=b""):
        """Send to every session except `exclude` (e.g. chat fan-out)."""
        with self._sessions_lock:
            sessions = [s for s in self._sessions if s is not exclude]
        for s in sessions:
            try:
                s.send(mtype, payload)
            except OSError:
                pass

    def _register(self, session):
        with self._sessions_lock:
            self._sessions.add(session)

    def _unregister(self, session):
        with self._sessions_lock:
            self._sessions.discard(session)
        st = session.put_state
        if st and st.get("fh"):
            try:
                st["fh"].close()
            except OSError:
                pass
        # a dropped client must not leave shells running
        try:
            self.terminals.close_for_owner(session)
        except Exception:  # noqa: BLE001
            pass

    # -- main loop ------------------------------------------------------------

    def serve_forever(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.bind, self.port))
        srv.listen(8)
        LOG.info("listening on %s:%d", self.bind or "0.0.0.0", self.port)
        stop = threading.Event()
        clip_thread = threading.Thread(target=self.clipboard.monitor_loop,
                                       args=(stop,), daemon=True)
        clip_thread.start()
        try:
            while True:
                conn, addr = srv.accept()
                LOG.info("connection from %s", addr[0])
                t = threading.Thread(target=self._serve_client, args=(conn,),
                                     daemon=True)
                t.start()
        finally:
            stop.set()

    def _serve_client(self, conn):
        device = {"device_id": "unknown", "device_name": "unknown", "platform": "unknown"}
        try:
            try:
                device = proto.server_handshake(conn, self.key, self.salt)
            except proto.AuthError as exc:
                LOG.warning("auth failed: %s", exc)
                try:
                    proto.send_msg(conn, proto.AUTH_FAIL, str(exc).encode())
                except OSError:
                    pass
                self.conn_log.log("auth_fail", device)
                return
            self.conn_log.log("connect", device)
            # Register the session BEFORE any logging/push I/O: a broadcast
            # (chat, clipboard) sent by another client in this window must
            # reach the new client. _unregister in the finally covers every
            # exit path below.
            session = _ClientSession(conn, device)
            self._register(session)
            try:
                if self.devices.trust(device):
                    LOG.info("new trusted device: %s (%s)",
                             device.get("device_id"), device.get("device_name"))
                self.conn_log.log("auth_ok", device)
                LOG.info("client authenticated: %s", device.get("device_id"))

                # push current clipboard so the viewer syncs on join
                try:
                    cur = self.clipboard.current_text()
                    if cur:
                        session.send(proto.CLIPBOARD_SET,
                                     json.dumps({"text": cur}).encode("utf-8"))
                except OSError:
                    pass
                stop = threading.Event()
                sender = None
                perms = self.devices.get_permissions(device.get("device_id")) or {}
                if perms.get("view", True):
                    sender = threading.Thread(target=self._frame_loop,
                                              args=(session, stop), daemon=True)
                    sender.start()
                else:
                    LOG.info("device %s has no view permission; no frames sent",
                             device.get("device_id"))
                try:
                    self._msg_loop(session, stop)
                finally:
                    stop.set()
                    if sender is not None:
                        sender.join(timeout=5)
            finally:
                self._unregister(session)
                self.conn_log.log("disconnect", device)
        except Exception as exc:  # noqa: BLE001 - one bad client must not kill us
            LOG.warning("client error: %s", exc)
        finally:
            try:
                conn.close()
            except OSError:
                pass
        LOG.info("client disconnected")

    def _frame_loop(self, session, stop):
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
                        session.send(proto.FRAME, frame)
                    except OSError:
                        break
                    last_hash = digest
                wait = interval - (time.monotonic() - t0)
                if wait > 0:
                    stop.wait(wait)
        finally:
            stop.set()

    def _msg_loop(self, session, stop):
        session.conn.settimeout(60)
        while not stop.is_set():
            try:
                mtype, payload = proto.recv_msg(session.conn)
            except (proto.ProtocolError, socket.timeout):
                break
            denied = self._perm_check(session, mtype, payload)
            if denied is not None:
                op, reason = denied
                LOG.warning("denied %s from %s: %s",
                            op, session.device.get("device_id"), reason)
                try:
                    session.send(proto.PERMS_DENIED,
                                 json.dumps({"op": op, "reason": reason}).encode("utf-8"))
                except OSError:
                    pass
                continue
            handler = self.handlers.get(mtype)
            if handler is None:
                LOG.warning("unexpected msg 0x%02x", mtype)
                continue
            try:
                done = handler(session, payload)
            except Exception as exc:  # noqa: BLE001 - bad payload, keep serving
                LOG.warning("handler 0x%02x failed: %s", mtype, exc)
                continue
            if done:
                break

    # -- permission enforcement (fail-closed, checked before handling) --------

    def _perm_check(self, session, mtype, payload):
        """Return (op, reason) if the message must be rejected, else None.

        On rejection the caller sends PERMS_DENIED and drops the message.
        Messages with no permission mapping (PING, DISCONNECT, CHAT_MSG,
        AGENT_QUERY, DISPLAYS_QUERY, PERMS_*, unknown types) are always
        allowed for authenticated clients.
        """
        device_id = session.device.get("device_id", "unknown")
        perms = self.devices.get_permissions(device_id)
        if perms is None:
            perms = {f: True for f in proto.PERMISSION_FLAGS}
        need = None
        op = "0x%02x" % mtype
        if mtype == proto.INPUT:
            op = "INPUT"
            try:
                t = json.loads(payload.decode("utf-8")).get("t")
            except ValueError:
                return (op, "malformed input event")
            if t in ("move", "click", "scroll"):
                need = "mouse"
            elif t == "key":
                need = "keyboard"
            else:
                return (op, "unknown input type %r" % (t,))
        elif mtype == proto.CLIPBOARD_SET:
            op, need = "CLIPBOARD_SET", "clipboard"
        elif mtype in (proto.FILE_LIST, proto.FILE_GET, proto.FILE_DATA,
                       proto.FILE_DONE, proto.FILE_PUT, proto.FILE_MKDIR,
                       proto.FILE_DELETE, proto.FILE_RENAME):
            op, need = "FILE_*", "files"
        elif mtype in (proto.TERMINAL_OPEN, proto.TERMINAL_DATA,
                       proto.TERMINAL_CLOSE):
            op, need = "TERMINAL_*", "terminal"
        elif mtype == proto.SYSTEM_CMD:
            op, need = "SYSTEM_CMD", "system"
        else:
            return None  # always allowed
        if need and not perms.get(need, False):
            return (op, "permission denied: %s" % need)
        return None

    # -- message handlers (one per type) --------------------------------------

    def _h_input(self, session, payload):
        try:
            ev = json.loads(payload.decode("utf-8"))
            self.injector.handle(ev)
        except (ValueError, KeyError, TypeError) as exc:
            LOG.warning("bad input event: %s", exc)
        return False

    def _h_ping(self, session, payload):
        session.send(proto.PONG, payload)
        return False

    def _h_disconnect(self, session, payload):
        return True

    def _h_clipboard(self, session, payload):
        try:
            text = json.loads(payload.decode("utf-8")).get("text", "")
        except ValueError:
            return False
        if not isinstance(text, str):
            return False
        LOG.debug("clipboard from %s (%d chars)",
                  session.device.get("device_id"), len(text))
        self.clipboard.set(text)
        return False

    # -- file transfer handlers ------------------------------------------------

    def _file_error(self, session, op, reason):
        try:
            session.send(proto.FILE_ERROR,
                         json.dumps({"op": op, "reason": reason}).encode("utf-8"))
        except OSError:
            pass

    def _h_file_list(self, session, payload):
        try:
            req = json.loads(payload.decode("utf-8"))
            entries = self.files.list_dir(req.get("path") or ".")
            session.send(proto.FILE_LIST_RESP, json.dumps({
                "path": req.get("path") or ".", "entries": entries,
            }).encode("utf-8"))
        except (ValueError, JailError, OSError) as exc:
            self._file_error(session, "list", str(exc))
        return False

    def _h_file_get(self, session, payload):
        try:
            req = json.loads(payload.decode("utf-8"))
            path = req.get("path", "")
            offset = int(req.get("offset") or 0)
            size, fh = self.files.open_read(path, offset)
        except (ValueError, JailError, OSError) as exc:
            self._file_error(session, "get", str(exc))
            return False
        try:
            session.send(proto.FILE_META,
                         json.dumps({"path": path, "size": size}).encode("utf-8"))
            while True:
                chunk = fh.read(FILE_CHUNK)
                if not chunk:
                    break
                session.send(proto.FILE_DATA, chunk)
            session.send(proto.FILE_DONE,
                         json.dumps({"path": path, "size": size}).encode("utf-8"))
        except OSError:
            pass
        finally:
            fh.close()
        return False

    def _h_file_put(self, session, payload):
        try:
            req = json.loads(payload.decode("utf-8"))
            path = req.get("path", "")
            size = int(req.get("size", 0))
            fh = self.files.begin_write(path, size)
        except (ValueError, JailError, OSError) as exc:
            self._file_error(session, "put", str(exc))
            return False
        # abort any previous incomplete upload on this session
        old = session.put_state
        if old and old.get("fh"):
            try:
                old["fh"].close()
            except OSError:
                pass
        session.put_state = {"path": path, "size": size, "received": 0, "fh": fh}
        LOG.info("upload started: %s (%d bytes)", path, size)
        return False

    def _h_file_data(self, session, payload):
        st = session.put_state
        if not st:
            return False  # stray chunk: ignore
        try:
            st["fh"].write(payload)
            st["received"] += len(payload)
        except OSError as exc:
            self._file_error(session, "put", str(exc))
            session.put_state = None
        return False

    def _h_file_done(self, session, payload):
        st = session.put_state
        if not st:
            return False
        try:
            st["fh"].close()
        except OSError:
            pass
        ok = st["received"] == st["size"]
        LOG.info("upload %s: %s (%d/%d bytes)",
                 "complete" if ok else "size mismatch",
                 st["path"], st["received"], st["size"])
        session.put_state = None
        if not ok:
            self._file_error(session, "put",
                             "size mismatch: got %d of %d" % (st["received"], st["size"]))
            return False
        try:
            session.send(proto.FILE_DONE,
                         json.dumps({"path": st["path"], "size": st["size"]}).encode("utf-8"))
        except OSError:
            pass
        return False

    def _h_file_mkdir(self, session, payload):
        try:
            req = json.loads(payload.decode("utf-8"))
            self.files.mkdir(req.get("path", ""))
            session.send(proto.FILE_DONE,
                         json.dumps({"path": req.get("path", ""), "size": 0}).encode("utf-8"))
        except (ValueError, JailError, OSError) as exc:
            self._file_error(session, "mkdir", str(exc))
        return False

    def _h_file_delete(self, session, payload):
        try:
            req = json.loads(payload.decode("utf-8"))
            self.files.delete(req.get("path", ""))
            session.send(proto.FILE_DONE,
                         json.dumps({"path": req.get("path", ""), "size": 0}).encode("utf-8"))
        except (ValueError, JailError, OSError) as exc:
            self._file_error(session, "delete", str(exc))
        return False

    def _h_file_rename(self, session, payload):
        try:
            req = json.loads(payload.decode("utf-8"))
            self.files.rename(req.get("from", ""), req.get("to", ""))
            session.send(proto.FILE_DONE,
                         json.dumps({"from": req.get("from", ""),
                                     "to": req.get("to", "")}).encode("utf-8"))
        except (ValueError, JailError, OSError) as exc:
            self._file_error(session, "rename", str(exc))
        return False

    # -- v3 handlers ------------------------------------------------------------

    def _h_system_cmd(self, session, payload):
        try:
            req = json.loads(payload.decode("utf-8"))
        except ValueError:
            return False
        resp = self.sys_cmd.execute(req)
        try:
            session.send(proto.SYSTEM_RESP, json.dumps(resp).encode("utf-8"))
        except OSError:
            pass
        LOG.info("system_cmd %s -> ok=%s", resp.get("cmd"), resp.get("ok"))
        return False

    def _h_terminal_open(self, session, payload):
        try:
            req = json.loads(payload.decode("utf-8"))
            cols = int(req.get("cols") or 80)
            rows = int(req.get("rows") or 24)
            sid = self.terminals.open(session.send, owner=session,
                                      cols=cols, rows=rows)
            resp = {"session": sid}
        except (ValueError, TerminalError) as exc:
            resp = {"session": 0, "error": str(exc)}
        try:
            session.send(proto.TERMINAL_OPENED, json.dumps(resp).encode("utf-8"))
        except OSError:
            pass
        return False

    def _h_terminal_data(self, session, payload):
        try:
            req = json.loads(payload.decode("utf-8"))
            sid = int(req["session"])
            raw = base64.b64decode(req["data"])
            self.terminals.write(sid, raw)
        except (ValueError, KeyError, TypeError, TerminalError) as exc:
            LOG.warning("bad terminal data: %s", exc)
        return False

    def _h_terminal_close(self, session, payload):
        try:
            req = json.loads(payload.decode("utf-8"))
            self.terminals.close(int(req["session"]))
        except (ValueError, KeyError, TypeError):
            pass
        return False

    def _h_chat(self, session, payload):
        try:
            msg = json.loads(payload.decode("utf-8"))
            sender = str(msg.get("from", session.device.get("device_id", "?")))[:128]
            text = msg.get("text", "")
            ts = msg.get("ts")
        except ValueError:
            return False
        if not isinstance(text, str):
            return False
        LOG.info("chat from %s (%d chars)", sender, len(text))
        self.chat.append(sender, text, ts)
        # fan out to every OTHER session; the sender does not get an echo
        self.broadcast_others(session, proto.CHAT_MSG, payload)
        return False

    def _h_agent_query(self, session, payload):
        try:
            req = json.loads(payload.decode("utf-8")) if payload else {}
        except ValueError:
            req = {}
        services = req.get("services") if isinstance(req, dict) else None
        if not isinstance(services, list):
            services = self.monitored_services
        status = collect_status(services)
        try:
            session.send(proto.AGENT_STATUS, json.dumps(status).encode("utf-8"))
        except OSError:
            pass
        return False

    def _h_displays_query(self, session, payload):
        info = get_displays(getattr(self.capture, "size", None))
        try:
            session.send(proto.DISPLAYS_LIST, json.dumps(info).encode("utf-8"))
        except OSError:
            pass
        return False

    # -- multi-user / permissions handlers ---------------------------------------

    @staticmethod
    def _valid_perms(perms):
        """All seven flags required, all booleans."""
        return (isinstance(perms, dict)
                and set(perms.keys()) == set(proto.PERMISSION_FLAGS)
                and all(isinstance(v, bool) for v in perms.values()))

    def _h_perms_set(self, session, payload):
        try:
            req = json.loads(payload.decode("utf-8"))
        except ValueError:
            return False
        target = req.get("device_id") if isinstance(req, dict) else None
        perms = req.get("permissions") if isinstance(req, dict) else None
        resp = {"device_id": target, "ok": False}
        own_id = session.device.get("device_id")
        if not isinstance(target, str) or not target:
            resp["detail"] = "device_id required"
        elif target == own_id:
            resp["detail"] = "cannot change own permissions"
        elif not self._valid_perms(perms):
            resp["detail"] = ("permissions must include all seven boolean flags: "
                              + ", ".join(proto.PERMISSION_FLAGS))
        elif not self.devices.set_permissions(target, perms):
            resp["detail"] = "unknown device"
        else:
            resp["ok"] = True
            LOG.info("permissions for %s set by %s: %s", target, own_id, perms)
        try:
            session.send(proto.PERMS_RESP, json.dumps(resp).encode("utf-8"))
        except OSError:
            pass
        return False

    def _h_perms_list(self, session, payload):
        devices = self.devices.list_devices()
        resp = {"devices": [
            {k: d.get(k) for k in ("device_id", "device_name", "platform",
                                   "first_seen", "last_seen", "permissions")}
            for d in devices
        ]}
        try:
            session.send(proto.PERMS_LIST_RESP, json.dumps(resp).encode("utf-8"))
        except OSError:
            pass
        return False


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
