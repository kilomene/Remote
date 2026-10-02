#!/usr/bin/env python3
"""host/capture.py -- screen capture backends for Remote 1.0.0 (Linux host).

Multi-backend frame sources behind one interface: every backend exposes
``grab() -> PIL.Image`` and a ``size`` property, plus a ``paused`` flag
honored by :func:`frame_loop`.

Backends
--------
* :class:`X11Capture` -- mss-based X11 capture (real; needs an X server).
* :class:`WaylandCapture` -- real xdg-desktop-portal ScreenCast handshake
  (in-process stdlib D-Bus client, because the PipeWire fd returned by
  OpenPipeWireRemote is delivered via SCM_RIGHTS and a one-shot ``gdbus``
  child process could never hand it to us) + frames through GStreamer
  ``pipewiresrc`` when ``gi.repository.Gst`` is importable. Raises
  RuntimeError with an honest message when the portal, the session bus,
  or GStreamer is absent.
* :class:`TestPatternCapture` -- synthetic frames for self-test (no
  display server needed).

Helpers: :func:`make_capture` (backend auto-select), :func:`list_displays`
(real ``xrandr --query`` parsing, ``[]`` when unavailable),
:func:`set_privacy` (best-effort ``xset dpms``), :class:`AdaptiveController`
(RTT-driven fps/JPEG-quality ladder, pure logic), and :func:`frame_loop`
(JPEG-byte generator honoring fps/pause/adaptive).

$0: stdlib only (+ PIL/mss via the vendored wheels, gi/Gst from the OS).
"""
import binascii
import collections
import logging
import os
import random
import re
import select
import shutil
import socket
import struct
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

LOG = logging.getLogger("remote-capture")

DEFAULT_FPS = 15
DEFAULT_QUALITY = 60


def ensure_vendor():
    """Make vendored pure-python wheels (mss) importable if the system
    does not provide them."""
    try:
        import mss  # noqa: F401
        return
    except ImportError:
        pass
    for d in (os.environ.get("REMOTE_VENDOR_DIR"), "/opt/remote/vendor"):
        if d and os.path.isdir(d):
            sys.path.insert(0, d)
            return


def encode_jpeg(img, quality=DEFAULT_QUALITY):
    import io
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=int(quality))
    return buf.getvalue()


# ---------------------------------------------------------------- X11


class X11Capture:
    """mss-based X11 capture. Raises RuntimeError if unavailable.

    ``display_id`` is an xrandr output name (see :func:`list_displays`);
    ``None`` captures the default monitor (mss monitor 1).
    """

    def __init__(self, display_id=None):
        ensure_vendor()
        try:
            import mss as mss_mod
        except ImportError as exc:
            raise RuntimeError("mss not available: %s" % exc)
        try:
            self._mss = mss_mod.mss()
        except Exception as exc:
            raise RuntimeError("mss cannot open the X display: %s" % exc)
        self.paused = False
        self._display_id = None
        self._mon = self._mss.monitors[1]
        if display_id is not None:
            self.set_display(display_id)
        LOG.info("capturing monitor %sx%s", self._mon["width"], self._mon["height"])

    @property
    def size(self):
        return self._mon["width"], self._mon["height"]

    @property
    def current_display(self):
        """The xrandr output name being captured, or None if unknown."""
        return self._display_id

    def set_display(self, display_id):
        """Switch the captured monitor. Validates *display_id* against
        real xrandr output names; raises ValueError/RuntimeError."""
        displays = list_displays()
        ids = [d["id"] for d in displays]
        if display_id not in ids:
            raise ValueError("unknown display %r (known: %s)"
                             % (display_id, ", ".join(ids) or "none"))
        want = displays[ids.index(display_id)]
        mons = self._mss.monitors[1:]  # [0] is the virtual full desktop
        # xrandr output order usually matches mss monitor order; verify by size.
        idx = ids.index(display_id)
        if idx < len(mons):
            cand = mons[idx]
            if cand["width"] == want["w"] and cand["height"] == want["h"]:
                self._mon = cand
                self._display_id = display_id
                LOG.info("capturing display %s (%sx%s)",
                         display_id, cand["width"], cand["height"])
                return
            LOG.warning("mss monitor order differs from xrandr; matching %s by size",
                        display_id)
        for m in mons:
            if m["width"] == want["w"] and m["height"] == want["h"]:
                self._mon = m
                self._display_id = display_id
                LOG.info("capturing display %s (%sx%s) via size match",
                         display_id, m["width"], m["height"])
                return
        raise RuntimeError("no mss monitor matches display %r (%dx%d)"
                           % (display_id, want["w"], want["h"]))

    def grab(self):
        shot = self._mss.grab(self._mon)
        from PIL import Image
        return Image.frombytes("RGB", shot.size, shot.rgb)


# ------------------------------------------------- minimal D-Bus client
# stdlib-only. Only what the ScreenCast handshake needs: SASL EXTERNAL
# auth, Hello, method calls, AddMatch, signals, and SCM_RIGHTS fd receipt
# (OpenPipeWireRemote returns a Unix fd -- the reason the handshake cannot
# go through the gdbus CLI: the fd would be delivered to the gdbus child
# process and die with it).

class _DBusError(RuntimeError):
    pass


def _parse_signature(sig):
    """Parse a D-Bus type signature into a token tree."""
    types = []
    i = [0]

    def _t():
        c = sig[i[0]]
        i[0] += 1
        if c == "a":
            if sig[i[0]] == "{":
                i[0] += 1
                k = _t()
                v = _t()
                assert sig[i[0]] == "}"
                i[0] += 1
                return ("a", ("e", k, v))
            return ("a", _t())
        if c == "(":
            fields = []
            while sig[i[0]] != ")":
                fields.append(_t())
            i[0] += 1
            return ("r", fields)
        if c == "v":
            return ("v",)
        return (c,)
    while i[0] < len(sig):
        types.append(_t())
    return types


def _align_of(t):
    c = t[0]
    if c in "ygv":
        return 1
    if c in "nq":
        return 2
    if c in "biu" or c in "soh":
        return 4
    return 8  # x, t, d, r (struct), e (dict entry), a (array)


class _DBusWriter:
    """Marshal D-Bus values (little-endian). Value model: scalars as
    int/bool/str, 'v' as (sig, value), 'a{sv}' as dict {str: (sig, value)},
    other arrays as lists, structs as lists/tuples, 'h' as int fd index."""

    def __init__(self):
        self.buf = bytearray()

    def pad(self, n):
        m = -len(self.buf) % n
        if m:
            self.buf.extend(b"\0" * m)

    def write(self, t, v):
        c = t[0]
        if c == "y":
            self.buf.append(v & 0xFF)
        elif c == "b":
            self.pad(4)
            self.buf.extend(struct.pack("<I", 1 if v else 0))
        elif c in "nq":
            self.pad(2)
            self.buf.extend(struct.pack("<H" if c == "q" else "<h", v))
        elif c in "iu":
            self.pad(4)
            self.buf.extend(struct.pack("<I" if c == "u" else "<i", v))
        elif c in "xt":
            self.pad(8)
            self.buf.extend(struct.pack("<Q" if c == "t" else "<q", v))
        elif c == "d":
            self.pad(8)
            self.buf.extend(struct.pack("<d", v))
        elif c in "so":
            b = v.encode("utf-8")
            self.pad(4)
            self.buf.extend(struct.pack("<I", len(b)))
            self.buf.extend(b)
            self.buf.append(0)
        elif c == "g":
            b = v.encode("ascii")
            if len(b) > 255:
                raise _DBusError("signature too long")
            self.buf.append(len(b))
            self.buf.extend(b)
            self.buf.append(0)
        elif c == "h":
            self.pad(4)
            self.buf.extend(struct.pack("<I", v))
        elif c == "v":
            sig, val = v
            self.write(("g",), sig)
            for st in _parse_signature(sig):
                self.write(st, val)
                break
        elif c == "a":
            et = t[1]
            tmp = _DBusWriter()
            if et[0] == "e":
                items = v.items() if isinstance(v, dict) else v
                for k, val in items:
                    tmp.pad(8)
                    tmp.write(et[1], k)
                    tmp.write(("v",), val)
            else:
                for item in v:
                    tmp.write(et, item)
            self.pad(4)
            lenpos = len(self.buf)
            self.buf.extend(b"\0\0\0\0")
            self.pad(_align_of(et))
            start = len(self.buf)
            self.buf.extend(tmp.buf)
            struct.pack_into("<I", self.buf, lenpos, len(self.buf) - start)
        elif c == "r":
            self.pad(8)
            for ft, fv in zip(t[1], v):
                self.write(ft, fv)
        elif c == "e":
            self.pad(8)
            self.write(t[1], v[0])
            self.write(("v",), v[1])
        else:
            raise _DBusError("cannot marshal type %r" % (c,))


class _DBusReader:
    """Demarshal D-Bus values (little-endian). 'h' values resolve to the
    real received fd numbers via the *fds* list."""

    def __init__(self, data, fds=()):
        self.d = data
        self.o = 0
        self.fds = list(fds)

    def pad(self, n):
        self.o += -self.o % n

    def read(self, t):
        c = t[0]
        if c == "y":
            v = self.d[self.o]
            self.o += 1
            return v
        if c == "b":
            self.pad(4)
            v = struct.unpack_from("<I", self.d, self.o)[0]
            self.o += 4
            return bool(v)
        if c in "nq":
            self.pad(2)
            v = struct.unpack_from("<H" if c == "q" else "<h", self.d, self.o)[0]
            self.o += 2
            return v
        if c in "iu":
            self.pad(4)
            v = struct.unpack_from("<I" if c == "u" else "<i", self.d, self.o)[0]
            self.o += 4
            return v
        if c in "xt":
            self.pad(8)
            v = struct.unpack_from("<Q" if c == "t" else "<q", self.d, self.o)[0]
            self.o += 8
            return v
        if c == "d":
            self.pad(8)
            v = struct.unpack_from("<d", self.d, self.o)[0]
            self.o += 8
            return v
        if c in "so":
            self.pad(4)
            ln = struct.unpack_from("<I", self.d, self.o)[0]
            self.o += 4
            b = bytes(self.d[self.o:self.o + ln])
            self.o += ln + 1  # trailing NUL
            return b.decode("utf-8")
        if c == "g":
            ln = self.d[self.o]
            self.o += 1
            b = bytes(self.d[self.o:self.o + ln])
            self.o += ln + 1
            return b.decode("ascii")
        if c == "h":
            self.pad(4)
            idx = struct.unpack_from("<I", self.d, self.o)[0]
            self.o += 4
            try:
                return self.fds[idx]
            except IndexError:
                raise _DBusError("fd index %d out of range (%d fds received)"
                                 % (idx, len(self.fds)))
        if c == "v":
            sig = self.read(("g",))
            return (sig, self.read(_parse_signature(sig)[0]))
        if c == "a":
            et = t[1]
            self.pad(4)
            ln = struct.unpack_from("<I", self.d, self.o)[0]
            self.o += 4
            end = self.o + ln
            self.pad(_align_of(et))
            out = []
            while self.o < end:
                if et[0] == "e":
                    self.pad(8)
                    k = self.read(et[1])
                    v = self.read(("v",))
                    out.append((k, v))
                else:
                    out.append(self.read(et))
            return out
        if c == "r":
            self.pad(8)
            return [self.read(ft) for ft in t[1]]
        if c == "e":
            self.pad(8)
            return (self.read(t[1]), self.read(("v",)))
        raise _DBusError("cannot demarshal type %r" % (c,))


# D-Bus message types / header field codes
_MSG_METHOD_CALL = 1
_MSG_METHOD_RETURN = 2
_MSG_ERROR = 3
_MSG_SIGNAL = 4
_F_PATH, _F_INTERFACE, _F_MEMBER = 1, 2, 3
_F_ERROR_NAME, _F_REPLY_SERIAL, _F_DESTINATION, _F_SIGNATURE = 4, 5, 6, 8


class _DBusConnection:
    """A connected D-Bus session-bus client (stdlib sockets only)."""

    def __init__(self, sock):
        self._sock = sock
        self._rbuf = bytearray()
        self._pending_fds = []
        self._serial = 1
        self._signals = []  # queued (path, interface, member, body)
        self.unique_name = None

    # -- transport ----------------------------------------------------

    @classmethod
    def open_session(cls):
        addr = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
        if not addr:
            rd = os.environ.get("XDG_RUNTIME_DIR")
            if rd:
                cand = os.path.join(rd, "bus")
                if os.path.exists(cand):
                    addr = "unix:path=" + cand
        if not addr:
            raise RuntimeError(
                "no D-Bus session bus: DBUS_SESSION_BUS_ADDRESS is unset and "
                "no $XDG_RUNTIME_DIR/bus exists")
        path = abstract = None
        for part in addr.split(";"):
            if part.startswith("unix:"):
                for kv in part[5:].split(","):
                    if kv.startswith("path="):
                        path = kv[5:]
                    elif kv.startswith("abstract="):
                        abstract = kv[9:]
                break
        if path is None and abstract is None:
            raise RuntimeError("unsupported D-Bus address %r (only unix:)" % addr)
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(10)
            s.connect(path if path is not None else "\0" + abstract)
        except OSError as exc:
            raise RuntimeError("cannot connect to session bus (%s): %s" % (addr, exc))
        self = cls(s)
        try:
            self._sasl()
            name = self.call("/org/freedesktop/DBus", "org.freedesktop.DBus",
                             "Hello", "", [], "org.freedesktop.DBus", timeout=10)
            self.unique_name = name[0]
        except Exception:
            try:
                s.close()
            except OSError:
                pass
            raise
        return self

    def _sasl_line(self):
        buf = b""
        while not buf.endswith(b"\r\n"):
            try:
                chunk = self._sock.recv(1)
            except OSError as exc:
                raise _DBusError("bus closed during SASL: %s" % exc)
            if not chunk:
                raise _DBusError("bus closed during SASL")
            buf += chunk
            if len(buf) > 4096:
                raise _DBusError("SASL line too long")
        return buf[:-2]

    def _sasl(self):
        uid_hex = binascii.hexlify(str(os.getuid()).encode("ascii"))
        self._sock.sendall(b"\0AUTH EXTERNAL " + uid_hex + b"\r\n")
        line = self._sasl_line()
        if not line.startswith(b"OK"):
            raise RuntimeError("D-Bus SASL EXTERNAL rejected: %r" % line[:80])
        self._sock.sendall(b"NEGOTIATE_UNIX_FD\r\n")
        line = self._sasl_line()
        if not line.startswith(b"AGREE_UNIX_FD"):
            raise RuntimeError("D-Bus server refused UNIX fd passing: %r" % line[:80])
        self._sock.sendall(b"BEGIN\r\n")

    def _fill(self):
        try:
            data, ancdata, _, _ = self._sock.recvmsg(
                65536, socket.CMSG_SPACE(24 * 4))
        except OSError as exc:
            raise _DBusError("bus read failed: %s" % exc)
        if not data:
            raise _DBusError("bus closed")
        self._rbuf += data
        for level, ctype, cmsg in ancdata:
            if level == socket.SOL_SOCKET and ctype == socket.SCM_RIGHTS:
                n = len(cmsg) // 4
                self._pending_fds.extend(struct.unpack("<%di" % n, cmsg))

    def _try_parse(self):
        if len(self._rbuf) < 16:
            return None
        hdr = bytes(self._rbuf[:16])
        if hdr[0:1] != b"l":
            raise _DBusError("unsupported byte order %r (only little-endian)"
                            % hdr[0:1])
        mtype, _flags = hdr[1], hdr[2]
        body_len, _serial = struct.unpack("<II", hdr[4:12])
        fields_len = struct.unpack("<I", hdr[12:16])[0]
        total = 16 + fields_len + body_len
        if len(self._rbuf) < total:
            return None
        raw = bytes(self._rbuf[:total])
        del self._rbuf[:total]
        fr = _DBusReader(raw[16:16 + fields_len])
        fields = {}
        for code, (fsig, fval) in fr.read(("a", ("e", ("y",), ("v",)))) or []:
            fields[code] = (fsig, fval)
        sig = fields.get(_F_SIGNATURE, ("g", ""))[1]
        body_raw = raw[16 + fields_len:]
        return (mtype,
                fields.get(_F_REPLY_SERIAL, ("u", 0))[1],
                fields.get(_F_PATH, ("o", ""))[1],
                fields.get(_F_INTERFACE, ("s", ""))[1],
                fields.get(_F_MEMBER, ("s", ""))[1],
                sig, body_raw)

    def _next_message(self, timeout):
        deadline = time.monotonic() + timeout
        while True:
            msg = self._try_parse()
            if msg is not None:
                return msg
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            r, _, _ = select.select([self._sock], [], [], remaining)
            if not r:
                return None
            self._fill()

    # -- calls / signals ----------------------------------------------

    def _send(self, mtype, fields, sig, body):
        fw = _DBusWriter()
        for code, (fsig, fval) in fields:
            fw.pad(8)
            fw.write(("y",), code)
            fw.write(("v",), (fsig, fval))
        bw = _DBusWriter()
        for t, v in zip(_parse_signature(sig), body):
            bw.write(t, v)
        hdr = bytearray()
        hdr.append(ord("l"))
        hdr.append(mtype)
        hdr.append(0)  # flags
        hdr.append(1)  # protocol version
        hdr.extend(struct.pack("<II", len(bw.buf), self._serial))
        hdr.extend(struct.pack("<I", len(fw.buf)))
        assert len(hdr) == 16
        try:
            self._sock.sendall(bytes(hdr) + bytes(fw.buf) + bytes(bw.buf))
        except OSError as exc:
            raise _DBusError("bus write failed: %s" % exc)
        serial = self._serial
        self._serial += 1
        return serial

    def call(self, path, iface, member, sig, args, dest, timeout=15):
        """Method call; returns the reply body values. 'h' (Unix fd) values
        in the reply resolve to the real received fd numbers."""
        serial = self._send(
            _MSG_METHOD_CALL,
            [(_F_PATH, ("o", path)), (_F_INTERFACE, ("s", iface)),
             (_F_MEMBER, ("s", member)), (_F_DESTINATION, ("s", dest)),
             (_F_SIGNATURE, ("g", sig))],
            sig, args)
        deadline = time.monotonic() + timeout
        n_fds_before = len(self._pending_fds)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("D-Bus call %s.%s timed out after %ss"
                                   % (iface, member, timeout))
            msg = self._next_message(remaining)
            if msg is None:
                raise RuntimeError("D-Bus call %s.%s timed out after %ss"
                                   % (iface, member, timeout))
            mtype, reply_serial, _p, _i, member_got, rsig, rraw = msg
            if mtype == _MSG_METHOD_RETURN and reply_serial == serial:
                # fds arriving with this reply (only OpenPipeWireRemote
                # carries any in this handshake's flow).
                fds = self._pending_fds[n_fds_before:]
                del self._pending_fds[n_fds_before:]
                body = []
                if rsig:
                    br = _DBusReader(rraw, fds)
                    for t in _parse_signature(rsig):
                        body.append(br.read(t))
                return body
            if mtype == _MSG_ERROR and reply_serial == serial:
                detail = ""
                if rsig:
                    br = _DBusReader(rraw)
                    vals = [br.read(t) for t in _parse_signature(rsig)]
                    detail = vals[0] if vals else ""
                raise RuntimeError("D-Bus error %s: %s" % (member_got, detail))
            if mtype == _MSG_SIGNAL:
                body = []
                if rsig:
                    br = _DBusReader(rraw)
                    for t in _parse_signature(rsig):
                        body.append(br.read(t))
                self._signals.append((_p, _i, member_got, body))

    def wait_signal(self, path, member, timeout):
        """Wait for a queued-or-incoming signal on *path*/*member*."""
        deadline = time.monotonic() + timeout
        while True:
            for i, (p, _iface, m, body) in enumerate(self._signals):
                if p == path and m == member:
                    del self._signals[i]
                    return body
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            msg = self._next_message(remaining)
            if msg is None:
                return None
            mtype, _rs, p, iface, m, rsig, rraw = msg
            if mtype == _MSG_SIGNAL:
                body = []
                if rsig:
                    br = _DBusReader(rraw)
                    for t in _parse_signature(rsig):
                        body.append(br.read(t))
                self._signals.append((p, iface, m, body))

    def close(self):
        try:
            self._sock.close()
        except OSError:
            pass


# ------------------------------------------------------- Wayland


class WaylandCapture:
    """Real xdg-desktop-portal ScreenCast capture.

    Handshake (CreateSession -> SelectSources -> Start -> OpenPipeWireRemote)
    runs over an in-process stdlib D-Bus client -- the PipeWire remote fd is
    delivered via SCM_RIGHTS, which a one-shot ``gdbus`` child process could
    never hand back to us (the fd would die with the child). Frames are
    pulled through GStreamer ``pipewiresrc`` when ``gi.repository.Gst`` is
    importable.

    Raises RuntimeError with an honest message when the session bus, the
    portal, or GStreamer is unavailable.
    """

    PORTAL_DEST = "org.freedesktop.portal.Desktop"
    PORTAL_PATH = "/org/freedesktop/portal/desktop"
    PORTAL_IFACE = "org.freedesktop.portal.ScreenCast"
    REQUEST_IFACE = "org.freedesktop.portal.Request"

    def __init__(self, monitor_index=0, cursor_mode=2):
        # 1: pre-flight via gdbus CLI (one-shot calls; no fd passing needed).
        self._portal_preflight()
        # 2: gi/Gst must exist before we bother the portal.
        Gst, GstVideo = self._init_gst()
        self._Gst, self._GstVideo = Gst, GstVideo
        self.paused = False
        bus = None
        try:
            bus = _DBusConnection.open_session()
            session, node = self._screencast_handshake(bus, monitor_index,
                                                      cursor_mode)
            pw_fd = self._open_pipewire_remote(bus, session)
        except Exception:
            if bus is not None:
                bus.close()
            raise
        self._bus = bus
        # 3: GStreamer pipewiresrc on the portal's PipeWire remote fd.
        self._pipe = Gst.parse_launch(
            "pipewiresrc fd=%d path=%u name=src ! "
            "videoconvert ! video/x-raw,format=RGB ! "
            "appsink name=sink emit-signals=false sync=false "
            "max-buffers=2 drop=true" % (pw_fd, node))
        self._pw_fd = pw_fd
        self._appsink = self._pipe.get_by_name("sink")
        self._pipe.set_state(Gst.State.PLAYING)
        w, h = self._first_frame_size(timeout_s=15)
        self._w, self._h = w, h
        LOG.info("wayland screencast streaming %dx%d (pipewire node %u)",
                 w, h, node)

    # -- setup steps ---------------------------------------------------

    @staticmethod
    def _portal_preflight():
        if shutil.which("gdbus") is None:
            raise RuntimeError(
                "gdbus not found on PATH: cannot probe the D-Bus session bus "
                "for xdg-desktop-portal")
        try:
            r = subprocess.run(
                ["gdbus", "call", "--session",
                 "--dest", "org.freedesktop.DBus",
                 "--object-path", "/org/freedesktop/DBus",
                 "--method", "org.freedesktop.DBus.NameHasOwner",
                 WaylandCapture.PORTAL_DEST],
                capture_output=True, timeout=10, text=True)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError("cannot talk to the D-Bus session bus: %s" % exc)
        if r.returncode != 0:
            detail = (r.stderr.strip() or r.stdout.strip() or "gdbus failed")[:200]
            raise RuntimeError("cannot talk to the D-Bus session bus: %s" % detail)
        if "(true,)" not in r.stdout:
            raise RuntimeError(
                "org.freedesktop.portal.Desktop is not on the session bus: "
                "xdg-desktop-portal is not running (Wayland screencast unavailable)")

    @staticmethod
    def _init_gst():
        try:
            import gi
            gi.require_version("Gst", "1.0")
            gi.require_version("GstVideo", "1.0")
            from gi.repository import Gst, GstVideo
        except (ImportError, ValueError) as exc:
            raise RuntimeError(
                "GStreamer gi bindings unavailable (%s): install python3-gi and "
                "gir1.2-gstreamer-1.0 for Wayland capture" % exc)
        Gst.init(None)
        return Gst, GstVideo

    def _request(self, bus, method, arg_sig, args, timeout):
        token = "remote%x" % random.getrandbits(32)
        body = bus.call(
            self.PORTAL_PATH, self.PORTAL_IFACE, method, arg_sig, args,
            self.PORTAL_DEST, timeout=timeout)
        req_path = body[0]
        rule = ("type='signal',sender='%s',path='%s',"
                "interface='%s',member='Response'"
                % (self.PORTAL_DEST, req_path, self.REQUEST_IFACE))
        bus.call("/org/freedesktop/DBus", "org.freedesktop.DBus",
                 "AddMatch", "s", [rule], "org.freedesktop.DBus",
                 timeout=10)
        sig_body = bus.wait_signal(req_path, "Response", timeout)
        if sig_body is None:
            raise RuntimeError(
                "portal %s timed out after %ss (dialog dismissed?)" % (method, timeout))
        response, results = sig_body[0], dict(sig_body[1])
        if response != 0:
            raise RuntimeError(
                "portal %s failed: response=%d (denied or cancelled)"
                % (method, response))
        return results, token

    def _screencast_handshake(self, bus, monitor_index, cursor_mode):
        # CreateSession
        results, _ = self._request(
            bus, "CreateSession", "a{sv}",
            [{"handle_token": ("s", "remote%x" % random.getrandbits(32)),
              "session_handle_token": ("s", "remote%x" % random.getrandbits(32))}],
            timeout=15)
        session = results["session_handle"][1]
        # SelectSources: monitors only; multiple when a non-zero index is wanted
        results, _ = self._request(
            bus, "SelectSources", "oa{sv}",
            [session, {"types": ("u", 1),
                       "multiple": ("b", bool(monitor_index)),
                       "cursor_mode": ("u", int(cursor_mode))}],
            timeout=60)
        # Start
        results, _ = self._request(
            bus, "Start", "ossa{sv}", [session, "", "", {}], timeout=60)
        streams = [(sid, dict(props)) for sid, props in results["streams"][1]]
        if monitor_index >= len(streams):
            raise RuntimeError(
                "portal offered %d stream(s); monitor_index %d out of range"
                % (len(streams), monitor_index))
        node = streams[monitor_index][1]["pipewire_node"][1]
        LOG.info("portal screencast: %d stream(s), using pipewire node %u",
                 len(streams), node)
        return session, node

    def _open_pipewire_remote(self, bus, session):
        """OpenPipeWireRemote returns a Unix fd ('h'); it arrives via
        SCM_RIGHTS on our own socket, which is exactly why this handshake
        runs in-process instead of through the gdbus CLI."""
        body = bus.call(self.PORTAL_PATH, self.PORTAL_IFACE,
                        "OpenPipeWireRemote", "oa{sv}", [session, {}],
                        self.PORTAL_DEST, timeout=15)
        fd = body[0]
        LOG.info("portal pipewire remote fd=%d", fd)
        return fd

    # -- capture API ---------------------------------------------------

    def _first_frame_size(self, timeout_s):
        Gst = self._Gst
        sample = self._appsink.try_pull_sample(timeout_s * Gst.SECOND)
        if sample is None:
            raise RuntimeError(
                "pipewiresrc produced no frame within %ss: portal stream stalled"
                % timeout_s)
        info = self._GstVideo.VideoInfo()
        if not info.from_caps(sample.get_caps()):
            raise RuntimeError("could not parse frame caps from pipewiresrc")
        return info.width, info.height

    @property
    def size(self):
        return self._w, self._h

    def grab(self):
        from PIL import Image
        Gst = self._Gst
        sample = self._appsink.try_pull_sample(2 * Gst.SECOND)
        if sample is None:
            raise RuntimeError("timed out waiting for a PipeWire frame")
        info = self._GstVideo.VideoInfo()
        if not info.from_caps(sample.get_caps()):
            raise RuntimeError("could not parse frame caps")
        buf = sample.get_buffer()
        ok, mapinfo = buf.map(Gst.MapFlags.READ)
        if not ok:
            raise RuntimeError("could not map frame buffer")
        try:
            raw = bytes(mapinfo.data)
        finally:
            buf.unmap(mapinfo)
        w, h, stride = info.width, info.height, info.stride[0]
        row = w * 3  # RGB
        if stride == row:
            return Image.frombytes("RGB", (w, h), raw)
        out = bytearray(row * h)
        astride = abs(stride)
        for y in range(h):
            src_y = (h - 1 - y) if stride < 0 else y
            out[y * row:(y + 1) * row] = raw[src_y * astride:src_y * astride + row]
        return Image.frombytes("RGB", (w, h), bytes(out))

    def close(self):
        for fn in (lambda: self._pipe.set_state(self._Gst.State.NULL),
                   lambda: os.close(self._pw_fd),
                   self._bus.close):
            try:
                fn()
            except Exception:
                pass

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


# ------------------------------------------------- test pattern


class TestPatternCapture:
    """Synthetic frames for self-test (no display server needed)."""

    def __init__(self, width=320, height=240):
        self.width, self.height = width, height
        self.n = 0
        self.paused = False

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


# ------------------------------------------------- multi-monitor


# "HDMI-1 connected primary 1920x1080+0+0 (normal ..." or
# "DP-2 connected 1920x1080+1920+0 (normal ..."
_XRANDR_LINE = re.compile(r"^(\S+)\s+connected\s+(primary\s+)?(?:(\d+)x(\d+)\+)?")


def list_displays():
    """List connected monitors from ``xrandr --query`` (real).

    Returns [{id, name, w, h, primary}, ...]; returns [] (no crash) when
    xrandr is missing or fails.
    """
    if shutil.which("xrandr") is None:
        return []
    try:
        out = subprocess.run(["xrandr", "--query"], capture_output=True,
                             timeout=5, text=True)
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    displays = []
    for line in out.stdout.splitlines():
        m = _XRANDR_LINE.match(line)
        if not m:
            continue
        name, primary, w, h = m.group(1), m.group(2), m.group(3), m.group(4)
        if not w or not h:
            continue  # connected but no active mode: skip
        displays.append({
            "id": name,
            "name": name,
            "w": int(w),
            "h": int(h),
            "primary": bool(primary and primary.strip()),
        })
    return displays


def make_capture(prefer=None):
    """Build the best working capture backend.

    *prefer*: "x11", "wayland", or None (auto). With no preference, a
    Wayland-only session tries Wayland first; otherwise X11 first.
    Raises RuntimeError (naming both failures) when nothing works.
    The synthetic TestPatternCapture is deliberately NOT a fallback here:
    make_capture promises real screen pixels.
    """
    errors = {}
    if prefer == "x11":
        order = ["x11", "wayland"]
    elif prefer == "wayland":
        order = ["wayland", "x11"]
    elif os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("DISPLAY"):
        order = ["wayland", "x11"]
    else:
        order = ["x11", "wayland"]
    for kind in order:
        try:
            if kind == "x11":
                return X11Capture()
            return WaylandCapture()
        except RuntimeError as exc:
            errors[kind] = str(exc)
            LOG.warning("%s capture unavailable: %s", kind, exc)
    raise RuntimeError("no working capture backend (x11: %s; wayland: %s)"
                       % (errors.get("x11", "?"), errors.get("wayland", "?")))


# ------------------------------------------------- adaptive streaming


class AdaptiveController:
    """RTT-driven (fps, jpeg_quality) ladder. Pure logic, no I/O.

    Ladder rungs run low->high; ``note_rtt(ms)`` feeds samples (kept for a
    30s window, >= 5 needed before adapting):
      * p95 RTT > 250ms  -> drop one rung immediately (floor: rung 0)
      * p95 RTT < 80ms   -> start/continue a good streak; raise one rung
        once the streak is 10s old (ceiling: top rung), then the streak
        restarts for the next raise.
    ``current`` -> (fps, jpeg_quality). ``clock`` is injectable for tests.
    """

    LADDER = ((15, 30), (15, 45), (30, 45), (30, 60),
              (45, 60), (45, 80), (60, 80))
    START_RUNG = 3  # (30 fps, q60)
    WINDOW_S = 30.0
    MIN_SAMPLES = 5
    DROP_P95_MS = 250.0
    RAISE_P95_MS = 80.0
    RAISE_HOLD_S = 10.0

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._rung = self.START_RUNG
        self._rtts = collections.deque()  # (timestamp, ms)
        self._good_since = None

    @property
    def rung(self):
        return self._rung

    @property
    def current(self):
        """(fps, jpeg_quality) for the current rung."""
        return self.LADDER[self._rung]

    def note_rtt(self, ms):
        now = self._clock()
        self._rtts.append((now, float(ms)))
        cutoff = now - self.WINDOW_S
        while self._rtts and self._rtts[0][0] < cutoff:
            self._rtts.popleft()
        self._adapt(now)

    def _p95(self):
        vals = sorted(ms for _, ms in self._rtts)
        if len(vals) < self.MIN_SAMPLES:
            return None
        idx = int(-(-95 * len(vals) // 100)) - 1  # ceil(0.95*n) - 1
        return vals[max(0, min(idx, len(vals) - 1))]

    def _adapt(self, now):
        p95 = self._p95()
        if p95 is None:
            return
        if p95 > self.DROP_P95_MS:
            self._good_since = None
            if self._rung > 0:
                self._rung -= 1
                LOG.info("adaptive: p95 rtt %.0fms > 250ms, dropping to %s",
                         p95, self.current)
            return
        if p95 < self.RAISE_P95_MS:
            if self._good_since is None:
                self._good_since = now
            elif now - self._good_since >= self.RAISE_HOLD_S:
                if self._rung < len(self.LADDER) - 1:
                    self._rung += 1
                    LOG.info("adaptive: p95 rtt %.0fms < 80ms for 10s, raising to %s",
                             p95, self.current)
                self._good_since = now
            return
        self._good_since = None  # middling: good streak broken


# ------------------------------------------------- privacy mode


def set_privacy(on):
    """Best-effort monitor power control via ``xset dpms``.

    Returns True only if xset existed and the command succeeded;
    False otherwise (honest -- never pretends).
    """
    xset = shutil.which("xset")
    if xset is None:
        return False
    try:
        r = subprocess.run([xset, "dpms", "force", "off" if on else "on"],
                           capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


# ------------------------------------------------- frame loop


def frame_loop(capture, adaptive=None, stop_event=None):
    """Yield JPEG bytes from ``capture.grab()``.

    Honors ``capture.paused`` (yields nothing while paused), the
    (fps, quality) from *adaptive* (defaults 15fps/q60), and stops when
    *stop_event* is set. Grab failures are logged and retried, never fatal.
    """
    import threading
    if stop_event is None:
        stop_event = threading.Event()
    while not stop_event.is_set():
        if getattr(capture, "paused", False):
            time.sleep(0.1)
            continue
        fps, quality = adaptive.current if adaptive is not None else (DEFAULT_FPS,
                                                                      DEFAULT_QUALITY)
        t0 = time.monotonic()
        try:
            yield encode_jpeg(capture.grab(), quality)
        except Exception:
            LOG.exception("capture grab failed; retrying")
            time.sleep(0.5)
            continue
        dt = time.monotonic() - t0
        time.sleep(max(0.0, 1.0 / float(fps) - dt))
