#!/usr/bin/env python3
"""remote-host: the controlled side of Remote (protocol v4, 1.0.0).

Listens on TCP (default 47800), runs the v4 pre-auth phase (DEVICE_HELLO,
optional PAIR_REQUEST pairing-code redemption, pairing policy), then the
password challenge-response handshake, then serves the session:

  * adaptive JPEG screen frames (X11, or Wayland via xdg-desktop-portal;
    FPS/quality ladder driven by measured send latency)
  * input injection (mouse/keyboard, incl. dblclick/hscroll/rel/text)
  * clipboard sync (text + image/png)
  * jailed file transfer
  * allowlisted system commands (+ launch-app via apps.py, pause-screen)
  * pty-backed remote terminal (resize, validated shell/env)
  * agent status (+ devtools), display listing, session chat
  * per-device 12-flag permissions, enforced fail-closed
  * remote audio (PulseAudio/PipeWire, Opus or PCM1 wire format)
  * webcam single-frame capture
  * Tailscale net status, privacy policy toggles, automation exec
  * camera-for-verification: phone H264 -> /dev/videoN via v4l2loopback

Message dispatch goes through host/plugins.py (PluginRegistry).
"""
import argparse
import base64
import datetime
import hashlib
import hmac
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "common"))

import remote_proto as proto
from files import FileTransfer, JailError, CHUNK as FILE_CHUNK
from sys_cmd import SysCmdExecutor
from terminal import TerminalManager, TerminalError
from monitor import collect_status
from displays import get_displays
from chat import ChatLog
from clipboard import ClipboardSync
from capture import (make_capture, TestPatternCapture, encode_jpeg,
                     set_privacy, AdaptiveController)
from input import InputInjector, TestInputSink
from auth import (get_or_create_device_id, PairingManager, RateLimiter,
                  SessionTokens, DeviceStore)
from net import net_status_payload
from policy import Policy
from jlog import JsonLogger
from plugins import PluginRegistry
from audio import AudioCapture, AudioError, list_sources as audio_list_sources
from webcam import list_cameras, grab_frame, WebcamError
from camera_virtual import VirtualCameraManager, CameraError
from automation import AutomationEngine
from devtools import detect_devtools

LOG = logging.getLogger("remote-host")

DEFAULT_CONFIG = "/etc/remote/host.conf"
DEFAULT_TRUSTED = "/etc/remote/trusted_devices"
DEFAULT_CONN_LOG = "/var/log/remote/connections.log"
DEFAULT_JLOG = "/var/log/remote/remote.jsonl"
DEFAULT_POLICY_CONF = "/etc/remote/policy.conf"
DEFAULT_PAIRING_PATH = "/etc/remote/pairing_codes"
DEFAULT_DEVICE_ID_PATH = "/etc/remote/device-id"
DEFAULT_COMMANDS_CONF = "/etc/remote/commands.conf"


def sd_notify(state):
    """Minimal systemd NOTIFY_SOCKET sender (stdlib only). Returns True when
    the datagram was accepted. No-op (False) when NOTIFY_SOCKET is unset."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            s.sendto(state.encode(), addr)
        finally:
            s.close()
        return True
    except OSError:
        return False


def _watchdog_loop(stop):
    """sd WATCHDOG=1 every 30s (the unit sets WatchdogSec=60)."""
    while not stop.wait(30):
        sd_notify("WATCHDOG=1")


def load_config(path):
    try:
        with open(path) as f:
            cfg = json.load(f)
        salt = base64.b64decode(cfg["salt"])
        key = base64.b64decode(cfg["key"])
        file_root = cfg.get("file_root") or os.path.expanduser("~")
        monitored = cfg.get("monitored_services") or ["remote-host", "tailscaled"]
        fps = cfg.get("fps") or 30
        if fps not in (15, 30, 45, 60):
            raise ValueError("fps must be one of 15/30/45/60")
        return salt, key, file_root, monitored, fps
    except (OSError, KeyError, ValueError) as exc:
        raise SystemExit("cannot load %s: %s (run remote-set-password first)" % (path, exc))


class _PreAuthStop(Exception):
    """Pre-auth denial that is not a password failure (blocked device,
    pairing required). rate_limit controls the RateLimiter recording."""

    def __init__(self, msg, rate_limit=False):
        super().__init__(msg)
        self.rate_limit = rate_limit


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


class _ClientSession:
    """Per-connection state."""

    def __init__(self, conn, device, peer_ip):
        self.conn = conn
        self.device = device
        self.peer_ip = peer_ip
        self.send_lock = threading.Lock()
        self.put_state = None  # {"path","size","received","fh"} during upload
        self.last_activity = time.monotonic()
        self.token = None
        self.audio = None       # AudioCapture while streaming
        self.audio_stop = None  # threading.Event for the audio pump

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
        self._privacy_on = False
        self.camera_owner = None  # session currently owning the virtual camera
        if self_test:
            salt = b"selftest-salt-16"  # exactly 16 bytes
            if test_password is None:
                raise SystemExit("--self-test needs --test-password or REMOTE_TEST_PASSWORD")
            self.key = proto.derive_key(test_password, salt)
            self.salt = salt
            self.file_root = os.environ.get("REMOTE_FILE_ROOT") or "/tmp/remote-selftest-files"
            self.fps = int(os.environ.get("REMOTE_FPS", "15"))
            self.capture = TestPatternCapture()
            self.injector = TestInputSink()
            self.devices = DeviceStore(os.environ.get("REMOTE_TRUSTED_FILE")
                                       or "/tmp/remote-selftest-trusted")
            self.conn_log = ConnectionLog(os.environ.get("REMOTE_CONN_LOG")
                                          or "/tmp/remote-selftest-connections.log")
            self.clipboard = ClipboardSync(self.broadcast, memory=True)
            self.policy = Policy(os.environ.get("REMOTE_POLICY_CONF")
                                 or "/tmp/remote-selftest-policy.conf")
            self.jlog = JsonLogger(os.environ.get("REMOTE_JLOG")
                                   or "/tmp/remote-selftest-remote.jsonl")
            self.pairing = PairingManager(os.environ.get("REMOTE_PAIRING_CODES")
                                          or "/tmp/remote-selftest-pairing.json")
            self.host_device_id = get_or_create_device_id(
                os.environ.get("REMOTE_DEVICE_ID")
                or "/tmp/remote-selftest-device-id")
            self.monitored_services = ["remote-host", "tailscaled"]
        else:
            self.salt, self.key, self.file_root, self.monitored_services, self.fps = \
                load_config(config_path)
            try:
                self.capture = make_capture()
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
            self.policy = Policy(DEFAULT_POLICY_CONF)
            self.jlog = JsonLogger(DEFAULT_JLOG)
            self.pairing = PairingManager(DEFAULT_PAIRING_PATH)
            self.host_device_id = get_or_create_device_id(DEFAULT_DEVICE_ID_PATH)

        self.rate_limiter = RateLimiter()
        self.session_tokens = SessionTokens()
        self.files = FileTransfer(self.file_root)
        LOG.info("file root: %s", self.files.root)

        # v3/v4 services
        self.sys_cmd = SysCmdExecutor(
            dry_run=self_test,
            displays_fn=lambda: get_displays(getattr(self.capture, "size", None)),
            monitored_services=self.monitored_services,
            pause_fn=self._pause_screen)
        self.terminals = TerminalManager()
        self.chat = ChatLog()
        self.automation = AutomationEngine(os.environ.get("REMOTE_COMMANDS_CONF")
                                          or DEFAULT_COMMANDS_CONF)
        self.camera = VirtualCameraManager()

        # Modular handler registry: one function per message type.
        # New protocol features register here without touching the loop.
        self.plugins = PluginRegistry()
        R = self.plugins.register
        R(proto.INPUT, self._h_input, "mouse")  # refined per-event in _perm_check
        R(proto.PING, self._h_ping)
        R(proto.DISCONNECT, self._h_disconnect)
        R(proto.CLIPBOARD_SET, self._h_clipboard, "clipboard")
        R(proto.FILE_LIST, self._h_file_list, "files")
        R(proto.FILE_GET, self._h_file_get, "files")
        R(proto.FILE_PUT, self._h_file_put, "files")
        R(proto.FILE_DATA, self._h_file_data, "files")
        R(proto.FILE_DONE, self._h_file_done, "files")
        R(proto.FILE_MKDIR, self._h_file_mkdir, "files")
        R(proto.FILE_DELETE, self._h_file_delete, "files")
        R(proto.FILE_RENAME, self._h_file_rename, "files")
        R(proto.SYSTEM_CMD, self._h_system_cmd, "system")  # launch-app -> apps
        R(proto.TERMINAL_OPEN, self._h_terminal_open, "terminal")
        R(proto.TERMINAL_DATA, self._h_terminal_data, "terminal")
        R(proto.TERMINAL_CLOSE, self._h_terminal_close, "terminal")
        R(proto.TERMINAL_RESIZE, self._h_terminal_resize, "terminal")
        R(proto.CHAT_MSG, self._h_chat)
        R(proto.AGENT_QUERY, self._h_agent_query)
        R(proto.DISPLAYS_QUERY, self._h_displays_query)
        R(proto.PERMS_SET, self._h_perms_set)
        R(proto.PERMS_LIST, self._h_perms_list)
        R(proto.AUDIO_START, self._h_audio_start, "audio")
        R(proto.AUDIO_STOP, self._h_audio_stop, "audio")
        R(proto.WEBCAM_LIST, self._h_webcam_list, "webcam")
        R(proto.WEBCAM_FRAME, self._h_webcam_frame, "webcam")
        R(proto.NET_STATUS, self._h_net_status)
        R(proto.POLICY_GET, self._h_policy_get)
        R(proto.EXEC_RUN, self._h_exec_run, "automation")
        R(proto.CAMERA_START, self._h_camera_start, "camera")
        R(proto.CAMERA_STOP, self._h_camera_stop, "camera")
        R(proto.CAMERA_FRAME, self._h_camera_frame, "camera")

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
            remaining = len(self._sessions)
        st = session.put_state
        if st and st.get("fh"):
            try:
                st["fh"].close()
            except OSError:
                pass
        # stop this session's audio stream, if any
        self._audio_stop_session(session)
        # tokens die with the connection
        try:
            self.session_tokens.revoke(id(session))
        except Exception:  # noqa: BLE001
            pass
        # the virtual camera is released when its owner goes away
        if self.camera_owner is session:
            try:
                self.camera.stop()
                self.jlog.log("camera", action="stop",
                              reason="owner disconnected",
                              device_id=session.device.get("device_id"))
            except Exception:  # noqa: BLE001
                pass
            self.camera_owner = None
        # privacy mode ends with the last session
        if remaining == 0 and self._privacy_on:
            try:
                set_privacy(False)
            except Exception:  # noqa: BLE001
                pass
            self._privacy_on = False
            LOG.info("privacy mode off (last client disconnected)")
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
        self._sweep_stale_camera_writers()
        LOG.info("listening on %s:%d", self.bind or "0.0.0.0", self.port)
        if sd_notify("READY=1"):
            LOG.info("sd_notify READY=1 sent")
        stop = threading.Event()
        for target, args in ((self.clipboard.monitor_loop, (stop,)),
                             (_watchdog_loop, (stop,)),
                             (self._camera_sweep_loop, (stop,))):
            t = threading.Thread(target=target, args=args, daemon=True)
            t.start()
        try:
            while True:
                conn, addr = srv.accept()
                LOG.info("connection from %s", addr[0])
                t = threading.Thread(target=self._serve_client, args=(conn,),
                                     daemon=True)
                t.start()
        finally:
            stop.set()

    def _camera_sweep_loop(self, stop):
        """10s sweep: auto-stop the virtual camera on inactivity."""
        while not stop.wait(10):
            try:
                if self.camera.check_inactivity():
                    self.camera_owner = None
                    self.jlog.log("camera", action="auto-stop",
                                  reason="inactivity timeout")
                    LOG.warning("camera auto-stopped (inactivity)")
            except Exception as exc:  # noqa: BLE001
                LOG.warning("camera sweep failed: %s", exc)

    def _sweep_stale_camera_writers(self):
        """Kill orphaned ffmpeg v4l2 writers left by a crashed host.

        A previous remote-host process that died without stop() leaves its
        ffmpeg child writing to a v4l2loopback device nobody reads. Match
        our exact writer signature (ffmpeg reading H264 from pipe:0 and
        writing to v4l2) and SIGTERM it. Best-effort; never raises.
        """
        if not os.path.isdir("/proc"):
            return
        me = os.getpid()
        killed = 0
        for pid in filter(str.isdigit, os.listdir("/proc")):
            if int(pid) == me:
                continue
            try:
                with open("/proc/%s/cmdline" % pid, "rb") as f:
                    parts = f.read().split(b"\0")
            except OSError:
                continue
            if not parts or not parts[0]:
                continue
            try:
                exe = os.path.basename(parts[0].decode("utf-8", "replace"))
            except ValueError:
                continue
            if (exe == "ffmpeg" and b"pipe:0" in parts and b"v4l2" in parts
                    and b"-f" in parts and b"h264" in parts):
                try:
                    os.kill(int(pid), signal.SIGTERM)
                    killed += 1
                    LOG.warning("camera: killed stale ffmpeg v4l2 writer pid %s "
                                "(orphaned by a previous host process)", pid)
                except (OSError, ValueError):
                    pass
        if killed:
            try:
                self.jlog.log("camera", action="stale-sweep", killed=killed)
            except Exception:  # noqa: BLE001
                pass

    # -- pre-auth: device hello, pairing codes, password -----------------------

    def _preauth(self, conn, peer_ip):
        """v4 pre-auth phase. Returns the device dict on success.

        1. DEVICE_HELLO (1.5s window; silent v1 clients proceed anonymous).
        2. Optional PAIR_REQUEST (1.5s window): redeem the pairing code;
           on success the device is trusted (marked paired for this session).
        3. Pairing policy: when require_pairing is set and the device is
           neither just-paired nor already trusted -> PAIR_REQUIRED, stop.
        4. Password challenge-response; AUTH_OK carries PROTOCOL_ID.

        Raises _PreAuthStop for policy denials, proto.AuthError for
        handshake/password failures.
        """
        device = {"device_id": "unknown", "device_name": "unknown",
                  "platform": "unknown"}
        conn.settimeout(1.5)
        try:
            mtype, payload = proto.recv_msg(conn)
            if mtype == proto.DEVICE_HELLO:
                try:
                    info = json.loads(payload.decode("utf-8"))
                    for k in ("device_id", "device_name", "platform"):
                        if info.get(k):
                            device[k] = str(info[k])[:128]
                except (ValueError, AttributeError):
                    pass
            else:
                raise proto.AuthError(
                    "expected DEVICE_HELLO or AUTH silence, got 0x%02x" % mtype)
        except socket.timeout:
            pass  # v1 client: silent, proceed without device info
        except proto.ProtocolError as exc:
            if "closed" in str(exc):
                raise proto.AuthError("connection closed before auth: %s" % exc)
            # any other framing weirdness pre-auth: proceed without device info

        if self.devices.is_blocked(device.get("device_id", "unknown")):
            raise _PreAuthStop("device is blocked", rate_limit=True)

        # optional pairing-code redemption (PairActivity flow)
        paired_now = False
        conn.settimeout(1.5)
        try:
            mtype, payload = proto.recv_msg(conn)
        except socket.timeout:
            mtype = None
        except proto.ProtocolError as exc:
            raise proto.AuthError("connection closed in pairing window: %s" % exc)
        if mtype is not None:
            if mtype != proto.PAIR_REQUEST:
                raise proto.AuthError(
                    "unexpected message 0x%02x in pairing window" % mtype)
            try:
                req = json.loads(payload.decode("utf-8"))
                code = str(req.get("code", ""))
                for k in ("device_id", "device_name", "platform"):
                    if req.get(k):
                        device[k] = str(req[k])[:128]
            except (ValueError, AttributeError):
                code = ""
            ok, detail = self.pairing.redeem(code)
            try:
                proto.send_msg(conn, proto.PAIR_RESULT,
                               json.dumps({"ok": ok, "detail": detail}).encode("utf-8"))
            except OSError:
                pass
            self.jlog.log("pairing", device_id=device.get("device_id"),
                          ok=ok, detail=detail, ip=peer_ip)
            LOG.info("pairing redeem from %s (%s): ok=%s (%s)",
                     peer_ip, device.get("device_id"), ok, detail)
            if ok:
                self.devices.trust(device)  # mark paired
                paired_now = True

        if (self.policy.check("pairing") and not paired_now
                and not self.devices.is_trusted(device.get("device_id", "unknown"))):
            try:
                proto.send_msg(conn, proto.PAIR_REQUIRED,
                               json.dumps({"device_id": self.host_device_id}).encode("utf-8"))
            except OSError:
                pass
            raise _PreAuthStop("pairing required for this host")

        # password challenge-response
        conn.settimeout(15.0)
        nonce = os.urandom(proto.NONCE_LEN)
        proto.send_msg(conn, proto.AUTH_REQ, self.salt + nonce)
        try:
            mtype, payload = proto.recv_msg(conn)
        except proto.ProtocolError as exc:
            raise proto.AuthError("no auth response: %s" % exc)
        if mtype != proto.AUTH_RESP:
            raise proto.AuthError("expected AUTH_RESP, got 0x%02x" % mtype)
        expected = hmac.new(self.key, nonce, hashlib.sha256).digest()
        if len(payload) != len(expected) or not hmac.compare_digest(payload, expected):
            raise proto.AuthError("bad password")
        proto.send_msg(conn, proto.AUTH_OK, proto.PROTOCOL_ID)
        conn.settimeout(None)
        device["_paired_now"] = paired_now
        return device

    def _request_approval(self, device):
        """Best-effort local approval dialog (60s). tkinter first, zenity
        fallback. Headless (no DISPLAY, no zenity) -> False: on a headless
        host only pairing-code sessions get in (documented in README)."""
        name = device.get("device_name", "unknown")
        did = device.get("device_id", "unknown")
        msg = "Allow remote access from %s (%s)?" % (name, did)
        if not os.environ.get("DISPLAY") and shutil.which("zenity") is None:
            LOG.warning("approval required for %s but no desktop session: "
                        "denying (pairing-code mode only)", did)
            return False
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            answer = [None]

            def ask():
                try:
                    answer[0] = messagebox.askyesno("Remote access request",
                                                    msg, parent=root)
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("approval dialog failed: %s", exc)
                finally:
                    try:
                        root.destroy()
                    except Exception:  # noqa: BLE001
                        pass

            t = threading.Thread(target=ask, daemon=True)
            t.start()
            t.join(60)
            if answer[0] is not None:
                return bool(answer[0])
        except Exception as exc:  # noqa: BLE001 - tkinter unavailable/broken
            LOG.warning("tkinter approval unavailable: %s", exc)
        try:
            r = subprocess.run(["zenity", "--question",
                                "--title=Remote access request",
                                "--text=" + msg, "--timeout=60"],
                               timeout=65)
            return r.returncode == 0
        except (OSError, subprocess.SubprocessError) as exc:
            LOG.warning("zenity approval failed: %s", exc)
        return False

    def _serve_client(self, conn):
        peer_ip = "unknown"
        try:
            peer_ip = conn.getpeername()[0]
        except OSError:
            pass
        device = {"device_id": "unknown", "device_name": "unknown",
                  "platform": "unknown"}
        try:
            allowed, retry_after = self.rate_limiter.allow(peer_ip)
            if not allowed:
                LOG.warning("rate-limited connection from %s (retry in %.0fs)",
                            peer_ip, retry_after)
                try:
                    proto.send_msg(conn, proto.AUTH_FAIL,
                                   b"too many failed attempts; try again later")
                except OSError:
                    pass
                self.conn_log.log("rate_limited",
                                  {"device_id": peer_ip, "device_name": peer_ip})
                return
            try:
                device = self._preauth(conn, peer_ip)
            except _PreAuthStop as exc:
                if exc.rate_limit:
                    self.rate_limiter.record_failure(peer_ip)
                LOG.warning("pre-auth denied for %s: %s", peer_ip, exc)
                try:
                    proto.send_msg(conn, proto.AUTH_FAIL, str(exc).encode())
                except OSError:
                    pass
                self.conn_log.log("auth_fail", device)
                self.jlog.log("auth", ok=False, reason=str(exc),
                              device_id=device.get("device_id"), ip=peer_ip)
                return
            except proto.AuthError as exc:
                self.rate_limiter.record_failure(peer_ip)
                LOG.warning("auth failed for %s: %s", peer_ip, exc)
                try:
                    proto.send_msg(conn, proto.AUTH_FAIL, str(exc).encode())
                except OSError:
                    pass
                self.conn_log.log("auth_fail", device)
                self.jlog.log("auth", ok=False, reason=str(exc),
                              device_id=device.get("device_id"), ip=peer_ip)
                return
            self.rate_limiter.record_success(peer_ip)
            device_id = device.get("device_id", "unknown")
            self.conn_log.log("connect", device)
            self.jlog.log("connection", action="connect", device_id=device_id,
                          device_name=device.get("device_name"), ip=peer_ip)
            # Register the session BEFORE any logging/push I/O: a broadcast
            # (chat, clipboard) sent by another client in this window must
            # reach the new client. _unregister in the finally covers every
            # exit path below.
            session = _ClientSession(conn, device, peer_ip)
            self._register(session)
            try:
                if self.devices.trust(device):
                    LOG.info("new trusted device: %s (%s)",
                             device_id, device.get("device_name"))
                self.conn_log.log("auth_ok", device)
                self.jlog.log("auth", ok=True, device_id=device_id,
                              device_name=device.get("device_name"), ip=peer_ip)
                LOG.info("client authenticated: %s", device_id)

                # session token, issued post AUTH_OK
                token, expires = self.session_tokens.issue(device_id, id(session))
                session.token = token
                try:
                    session.send(proto.SESSION_TOKEN,
                                 json.dumps({"token": token,
                                             "expires_in": expires}).encode("utf-8"))
                except OSError:
                    return
                rot_stop = threading.Event()
                rot = threading.Thread(target=self._token_rotator,
                                       args=(session, rot_stop), daemon=True)
                rot.start()
                try:
                    # approval gate (pairing-code sessions bypass: the code
                    # was the approval)
                    if self.policy.check("approval") and not device.get("_paired_now"):
                        if self._request_approval(device):
                            self.jlog.log("approval", ok=True, device_id=device_id)
                            LOG.info("session approved for %s", device_id)
                        else:
                            self.jlog.log("approval", ok=False, device_id=device_id)
                            LOG.warning("session denied for %s (no approval)",
                                        device_id)
                            return
                    # privacy mode on first connect
                    with self._sessions_lock:
                        first = len(self._sessions) == 1
                    if first and self.policy.get("hide_on_connect") \
                            and not self._privacy_on:
                        if set_privacy(True):
                            self._privacy_on = True
                            LOG.info("privacy mode on (hide_on_connect)")

                    # push current clipboard so the viewer syncs on join
                    try:
                        cur = self.clipboard.current_payload()
                        if cur:
                            session.send(proto.CLIPBOARD_SET,
                                         json.dumps(cur).encode("utf-8"))
                    except OSError:
                        pass
                    stop = threading.Event()
                    sender = None
                    perms = self.devices.get_permissions(device_id) or {}
                    if perms.get("view", True):
                        sender = threading.Thread(target=self._frame_loop,
                                                  args=(session, stop), daemon=True)
                        sender.start()
                    else:
                        LOG.info("device %s has no view permission; no frames sent",
                                 device_id)
                    try:
                        self._msg_loop(session, stop)
                    finally:
                        stop.set()
                        rot_stop.set()
                        if sender is not None:
                            sender.join(timeout=5)
                finally:
                    rot_stop.set()
            finally:
                self._unregister(session)
                self.conn_log.log("disconnect", device)
                self.jlog.log("connection", action="disconnect",
                              device_id=device.get("device_id"), ip=peer_ip)
        except Exception as exc:  # noqa: BLE001 - one bad client must not kill us
            LOG.warning("client error: %s", exc)
            try:
                self.jlog.log("error", detail=str(exc)[:200], ip=peer_ip)
            except Exception:  # noqa: BLE001
                pass
        finally:
            try:
                conn.close()
            except OSError:
                pass
        LOG.info("client disconnected")

    def _token_rotator(self, session, stop):
        """Hourly SESSION_ROTATE for one session."""
        while not stop.wait(3600):
            try:
                token, expires = self.session_tokens.rotate(
                    session.device.get("device_id", "unknown"), id(session))
                session.token = token
                session.send(proto.SESSION_ROTATE,
                             json.dumps({"token": token,
                                         "expires_in": expires}).encode("utf-8"))
                LOG.debug("rotated session token for %s",
                          session.device.get("device_id"))
            except OSError:
                break
            except Exception as exc:  # noqa: BLE001
                LOG.warning("token rotation failed: %s", exc)
                break

    def _frame_loop(self, session, stop):
        adaptive = AdaptiveController()
        # config "fps" (15/30/45/60) selects the starting ladder rung
        rung = next((i for i, (f, _q) in enumerate(AdaptiveController.LADDER)
                     if f == self.fps), AdaptiveController.START_RUNG)
        adaptive._rung = rung  # same codebase; starting point only
        last_hash = None
        try:
            while not stop.is_set():
                if getattr(self.capture, "paused", False):
                    stop.wait(0.1)
                    continue
                fps, quality = adaptive.current
                t0 = time.monotonic()
                try:
                    frame = encode_jpeg(self.capture.grab(), quality)
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("capture failed: %s", exc)
                    stop.wait(1.0)
                    continue
                digest = hashlib.sha256(frame).digest()
                if digest != last_hash:
                    t_send = time.monotonic()
                    try:
                        session.send(proto.FRAME, frame)
                    except OSError:
                        break
                    # measured socket-send latency is the congestion signal
                    # (pragmatic and real: a slow peer backs up the send)
                    adaptive.note_rtt((time.monotonic() - t_send) * 1000.0)
                    last_hash = digest
                wait = 1.0 / fps - (time.monotonic() - t0)
                if wait > 0:
                    stop.wait(wait)
        finally:
            stop.set()

    def _msg_loop(self, session, stop):
        session.conn.settimeout(60)
        idle_limit = (self.policy.get("idle_disconnect_min") or 0) * 60
        while not stop.is_set():
            try:
                mtype, payload = proto.recv_msg(session.conn)
            except socket.timeout:
                # quiet period: enforce the idle-disconnect policy, if any
                if idle_limit and \
                        time.monotonic() - session.last_activity > idle_limit:
                    LOG.info("idle disconnect for %s",
                             session.device.get("device_id"))
                    self.jlog.log("connection", action="idle-disconnect",
                                  device_id=session.device.get("device_id"))
                    break
                continue
            except proto.ProtocolError:
                break
            session.last_activity = time.monotonic()
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
            handler, _flag = self.plugins.lookup(mtype)
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
        The permission-flag mapping lives in the PluginRegistry; the
        special cases below refine it (INPUT event granularity, SYSTEM_CMD
        launch-app -> "apps"), and the policy master switches can disable
        whole features regardless of per-device flags.
        """
        device_id = session.device.get("device_id", "unknown")
        handler, need = self.plugins.lookup(mtype)
        op = "0x%02x" % mtype
        if mtype == proto.INPUT:
            op = "INPUT"
            try:
                t = json.loads(payload.decode("utf-8")).get("t")
            except ValueError:
                return (op, "malformed input event")
            if t in ("move", "click", "scroll", "dblclick", "hscroll", "rel"):
                need = "mouse"
            elif t in ("key", "text"):
                need = "keyboard"
            else:
                return (op, "unknown input type %r" % (t,))
        elif mtype == proto.SYSTEM_CMD:
            op = "SYSTEM_CMD"
            try:
                cmd = json.loads(payload.decode("utf-8")).get("cmd") if payload else None
            except ValueError:
                cmd = None
            need = "apps" if cmd == "launch-app" else "system"
        elif mtype in (proto.FILE_LIST, proto.FILE_GET, proto.FILE_DATA,
                       proto.FILE_DONE, proto.FILE_PUT, proto.FILE_MKDIR,
                       proto.FILE_DELETE, proto.FILE_RENAME):
            op, need = "FILE_*", "files"
        elif mtype in (proto.TERMINAL_OPEN, proto.TERMINAL_DATA,
                       proto.TERMINAL_CLOSE, proto.TERMINAL_RESIZE):
            op, need = "TERMINAL_*", "terminal"
        elif mtype == proto.CLIPBOARD_SET:
            op, need = "CLIPBOARD_SET", "clipboard"
        elif mtype in (proto.AUDIO_START, proto.AUDIO_STOP):
            op, need = "AUDIO_*", "audio"
        elif mtype in (proto.WEBCAM_LIST, proto.WEBCAM_FRAME):
            op, need = "WEBCAM_*", "webcam"
        elif mtype == proto.EXEC_RUN:
            op, need = "EXEC_RUN", "automation"
        elif mtype in (proto.CAMERA_START, proto.CAMERA_STOP,
                       proto.CAMERA_FRAME):
            op, need = "CAMERA_*", "camera"
        elif handler is None:
            return None  # unknown types: allowed through to the warning
        # policy master switches (fail closed; denial names the policy)
        policy_feature = None
        if op == "FILE_*":
            policy_feature = "files"
        elif op == "TERMINAL_*":
            policy_feature = "terminal"
        elif mtype == proto.CLIPBOARD_SET:
            policy_feature = "clipboard"
        elif mtype in (proto.AUDIO_START, proto.AUDIO_STOP):
            policy_feature = "audio"
        if policy_feature and not self.policy.check(policy_feature):
            return (op, "disabled by policy")
        if need:
            perms = self.devices.get_permissions(device_id)
            if perms is None:
                perms = {f: True for f in proto.PERMISSION_FLAGS}
            if not perms.get(need, False):
                return (op, "permission denied: %s" % need)
        return None

    # -- capture pause (SYSTEM_CMD "pause-screen") -----------------------------

    def _pause_screen(self, paused):
        """pause_fn for SysCmdExecutor: pause/resume frame streaming."""
        try:
            self.capture.paused = bool(paused)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("pause-screen failed: %s", exc)
            return
        LOG.info("screen %s", "paused" if paused else "resumed")

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
            obj = json.loads(payload.decode("utf-8"))
        except ValueError:
            return False
        LOG.debug("clipboard from %s", session.device.get("device_id"))
        self.clipboard.set_from_wire(obj)
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

    # -- system / terminal / chat / agent / displays ----------------------------

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
        self.jlog.log("command", cmd=resp.get("cmd"), ok=resp.get("ok"),
                      device_id=session.device.get("device_id"))
        return False

    def _h_terminal_open(self, session, payload):
        try:
            req = json.loads(payload.decode("utf-8"))
            cols = int(req.get("cols") or 80)
            rows = int(req.get("rows") or 24)
            shell = req.get("shell")
            env = req.get("env")
            if env is not None and not isinstance(env, dict):
                raise TerminalError("env must be an object")
            sid = self.terminals.open(session.send, owner=session,
                                      cols=cols, rows=rows,
                                      shell=shell, env=env)
            resp = {"session": sid}
        except (ValueError, TypeError, TerminalError) as exc:
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

    def _h_terminal_resize(self, session, payload):
        try:
            req = json.loads(payload.decode("utf-8"))
            self.terminals.resize(int(req["session"]),
                                  int(req.get("cols") or 80),
                                  int(req.get("rows") or 24))
        except (ValueError, KeyError, TypeError, TerminalError) as exc:
            LOG.warning("bad terminal resize: %s", exc)
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
            status["devtools"] = detect_devtools()
        except Exception as exc:  # noqa: BLE001 - probes must not break us
            LOG.warning("devtools probe failed: %s", exc)
            status["devtools"] = {}
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

    # -- multi-user / permissions -------------------------------------------------

    @staticmethod
    def _valid_perms(perms):
        """All twelve flags required, all booleans."""
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
            resp["detail"] = ("permissions must include all twelve boolean flags: "
                              + ", ".join(proto.PERMISSION_FLAGS))
        elif not self.devices.set_permissions(target, perms):
            resp["detail"] = "unknown device"
        else:
            resp["ok"] = True
            LOG.info("permissions for %s set by %s: %s", target, own_id, perms)
            self.jlog.log("permission-change", device_id=target,
                          by=own_id, permissions=perms)
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

    # -- v4: remote audio ----------------------------------------------------------

    def _audio_stop_session(self, session):
        """Stop this session's audio stream, if any. Idempotent."""
        cap, stop = session.audio, session.audio_stop
        session.audio, session.audio_stop = None, None
        if stop is not None:
            stop.set()
        if cap is not None:
            try:
                cap.stop()
            except Exception:  # noqa: BLE001
                pass

    def _h_audio_start(self, session, payload):
        try:
            req = json.loads(payload.decode("utf-8")) if payload else {}
        except ValueError:
            req = {}
        source = req.get("source") if isinstance(req, dict) else None
        # a second AUDIO_START replaces the first (one stream per session)
        self._audio_stop_session(session)
        cap = AudioCapture()
        try:
            if not source:
                sources = audio_list_sources()
                monitors = [s for s in sources if s.get("kind") == "monitor"]
                pick = monitors or sources
                if not pick:
                    raise AudioError("no audio sources on this host")
                source = pick[0]["id"]
            cap.start(source)
        except AudioError as exc:
            try:
                session.send(proto.AUDIO_ERROR,
                             json.dumps({"detail": str(exc)}).encode("utf-8"))
            except OSError:
                pass
            return False
        stop = threading.Event()
        session.audio, session.audio_stop = cap, stop
        t = threading.Thread(target=self._audio_pump,
                             args=(session, cap, stop), daemon=True)
        t.start()
        LOG.info("audio streaming to %s (source %s)",
                 session.device.get("device_id"), source)
        return False

    def _audio_pump(self, session, cap, stop):
        while not stop.is_set():
            try:
                chunk = cap.read_chunk()
            except AudioError as exc:
                LOG.warning("audio capture error: %s", exc)
                try:
                    session.send(proto.AUDIO_ERROR,
                                 json.dumps({"detail": str(exc)}).encode("utf-8"))
                except OSError:
                    pass
                break
            try:
                session.send(proto.AUDIO_DATA, chunk)
            except OSError:
                break
        cap.stop()

    def _h_audio_stop(self, session, payload):
        self._audio_stop_session(session)
        return False

    # -- v4: webcam -----------------------------------------------------------------

    def _h_webcam_list(self, session, payload):
        cams = list_cameras()
        try:
            session.send(proto.WEBCAM_LIST,
                         json.dumps({"cameras": cams}).encode("utf-8"))
        except OSError:
            pass
        return False

    def _h_webcam_frame(self, session, payload):
        try:
            req = json.loads(payload.decode("utf-8"))
            cam_id = req.get("id") if isinstance(req, dict) else None
        except ValueError:
            cam_id = None
        try:
            jpeg = grab_frame(cam_id)
        except WebcamError as exc:
            # documented in PROTOCOL.md: error JSON on the same type; the
            # client branches on the first bytes (JPEG starts FF D8).
            body = json.dumps({"error": str(exc)}).encode("utf-8")
        else:
            body = jpeg
        try:
            session.send(proto.WEBCAM_FRAME, body)
        except OSError:
            pass
        return False

    # -- v4: net status / policy / automation ------------------------------------------

    def _h_net_status(self, session, payload):
        peer_ip = session.peer_ip

        def work():
            try:
                body = net_status_payload(peer_ip=peer_ip)
                session.send(proto.NET_STATUS,
                             json.dumps(body).encode("utf-8"))
            except OSError:
                pass

        # tailscale ping can take seconds; never stall the message loop
        threading.Thread(target=work, daemon=True).start()
        return False

    def _h_policy_get(self, session, payload):
        try:
            session.send(proto.POLICY_GET,
                         json.dumps({"toggles": self.policy.as_dict()}).encode("utf-8"))
        except OSError:
            pass
        return False

    def _h_exec_run(self, session, payload):
        try:
            req = json.loads(payload.decode("utf-8"))
            name = req.get("name") if isinstance(req, dict) else None
            args = req.get("args") if isinstance(req, dict) else None
        except ValueError:
            return False
        res = self.automation.run(name, args)
        try:
            session.send(proto.EXEC_RESULT, json.dumps(res).encode("utf-8"))
        except OSError:
            pass
        return False

    # -- v4: camera for verification -----------------------------------------------------

    def _h_camera_start(self, session, payload):
        try:
            req = json.loads(payload.decode("utf-8"))
            width = int(req.get("width") or 0)
            height = int(req.get("height") or 0)
            fps = int(req.get("fps") or 0)
            facing = req.get("facing")
            rotation = int(req.get("rotation") or 0)
            mirror = bool(req.get("mirror"))
            dev = self.camera.start(width, height, fps, facing,
                                    rotation=rotation, mirror=mirror)
        except (ValueError, TypeError, AttributeError, CameraError) as exc:
            err = str(exc)
            self.jlog.log("camera", action="start", ok=False, error=err,
                          device_id=session.device.get("device_id"))
            status = {"active": False, "device": self.camera.device or "",
                      "width": 0, "height": 0, "fps": 0, "error": err}
        else:
            self.camera_owner = session
            self.jlog.log("camera", action="start", ok=True, device=dev,
                          width=width, height=height, fps=fps, facing=facing,
                          rotation=rotation, mirror=mirror,
                          device_id=session.device.get("device_id"))
            status = self.camera.status_dict()
        try:
            session.send(proto.CAMERA_STATUS, json.dumps(status).encode("utf-8"))
        except OSError:
            pass
        return False

    def _h_camera_frame(self, session, payload):
        if self.camera_owner is not None and self.camera_owner is not session:
            err = "camera owned by another session"
        else:
            try:
                self.camera.write_frame(payload)
                return False
            except CameraError as exc:
                err = str(exc)
                if not self.camera.active:
                    self.camera_owner = None
        try:
            status = dict(self.camera.status_dict())
            status["error"] = err
            session.send(proto.CAMERA_STATUS, json.dumps(status).encode("utf-8"))
        except OSError:
            pass
        return False

    def _h_camera_stop(self, session, payload):
        if self.camera_owner is not None and self.camera_owner is not session:
            # not the owner: report status, never stop someone else's stream
            try:
                session.send(proto.CAMERA_STATUS,
                             json.dumps(self.camera.status_dict()).encode("utf-8"))
            except OSError:
                pass
            return False
        was_active = self.camera.active
        self.camera.stop()
        self.camera_owner = None
        if was_active:
            self.jlog.log("camera", action="stop",
                          device_id=session.device.get("device_id"))
        try:
            session.send(proto.CAMERA_STATUS,
                         json.dumps(self.camera.status_dict()).encode("utf-8"))
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
