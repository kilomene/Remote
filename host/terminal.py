"""host/terminal.py -- remote terminal sessions for Remote v4.

TERMINAL_OPEN (0x61 c->s) opens a login shell on a fresh pty and replies
TERMINAL_OPENED (0x61 s->c) {session}. A pump thread per session forwards
pty output as TERMINAL_DATA (0x62, base64 of raw pty bytes); client input
arrives as TERMINAL_DATA and is written to the pty master. Either side may
send TERMINAL_CLOSE (0x63).

v4: TERMINAL_RESIZE (0x69 c->s) {session, cols, rows} resizes the pty via
the real TIOCSWINSZ ioctl; open() accepts shell= (validated against
/etc/shells) and env= (sanitized, merged over os.environ).

Concurrent sessions are capped at MAX_SESSIONS (8). The manager is
thread-safe; the send callback is invoked from pump threads (the host's
session.send is already send_lock-guarded).
"""
import base64
import fcntl
import json
import logging
import os
import pty
import re
import select
import signal
import struct
import subprocess
import threading

try:
    from termios import TIOCSWINSZ
except ImportError:  # non-tty platforms; resize() will raise honestly
    TIOCSWINSZ = None

import remote_proto as proto

LOG = logging.getLogger("remote-host")

MAX_SESSIONS = 8
READ_CHUNK = 65536


class TerminalError(Exception):
    pass


class TerminalManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._sessions = {}  # sid -> {"master", "proc", "stop", "thread", "owner"}
        self._next_id = 1

    # -- shell / env validation -------------------------------------------------

    ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

    @staticmethod
    def _valid_shells():
        """Set of absolute paths listed in /etc/shells (plus safe fallbacks)."""
        shells = set()
        try:
            with open("/etc/shells") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        shells.add(line)
        except OSError:
            pass
        shells.update(["/bin/sh", "/bin/bash"])
        return shells

    @staticmethod
    def _resolve_shell(shell):
        """Validate an explicit shell against /etc/shells; resolve the default.

        Raises TerminalError on anything not explicitly valid.
        """
        valid = TerminalManager._valid_shells()
        if shell is None:
            for cand in (os.environ.get("SHELL"), "/bin/bash", "/bin/sh"):
                if cand and cand in valid and os.access(cand, os.X_OK):
                    return cand
            raise TerminalError("no usable shell found")
        if not isinstance(shell, str) or not shell.startswith("/"):
            raise TerminalError("shell must be an absolute path, got %r" % (shell,))
        if shell not in valid:
            raise TerminalError("shell %r is not listed in /etc/shells" % shell)
        if not os.access(shell, os.X_OK):
            raise TerminalError("shell %r is not executable" % shell)
        return shell

    @staticmethod
    def _merge_env(env):
        """Merge caller-supplied env vars over os.environ.

        Keys must match [A-Za-z_][A-Za-z0-9_]* and values must be strings;
        anything else raises TerminalError.
        """
        merged = dict(os.environ)
        if env:
            if not isinstance(env, dict):
                raise TerminalError("env must be a dict")
            for key, val in env.items():
                if not isinstance(key, str) or \
                        not TerminalManager.ENV_KEY_RE.match(key):
                    raise TerminalError("invalid env var name %r" % (key,))
                if not isinstance(val, str):
                    raise TerminalError("env var %r value must be a string" % key)
                merged[key] = val
        merged["TERM"] = "xterm-256color"
        return merged

    @staticmethod
    def _apply_winsize(master_fd, cols, rows):
        """Best-effort TIOCSWINSZ on a pty master fd."""
        if TIOCSWINSZ is None:
            return False
        try:
            fcntl.ioctl(master_fd, TIOCSWINSZ,
                        struct.pack("HHHH", rows, cols, 0, 0))
            return True
        except OSError:
            return False

    # -- lifecycle -------------------------------------------------------------

    def open(self, send, owner=None, cols=80, rows=24, shell=None, env=None):
        """Start a login shell on a new pty. send(mtype, payload) is called
        from the pump thread. Returns the integer session id.

        shell: absolute path, must be listed in /etc/shells (default: $SHELL
               or /bin/bash). env: dict of extra env vars merged over
               os.environ (keys sanitized). Raises TerminalError on any
               validation failure.
        """
        cols = max(20, min(500, int(cols or 80)))
        rows = max(5, min(200, int(rows or 24)))
        shell = self._resolve_shell(shell)
        env = self._merge_env(env)
        env["COLUMNS"] = str(cols)
        env["LINES"] = str(rows)
        with self._lock:
            if len(self._sessions) >= MAX_SESSIONS:
                raise TerminalError("too many terminal sessions (max %d)" % MAX_SESSIONS)
            sid = self._next_id
            self._next_id += 1
        try:
            master, slave = pty.openpty()
        except OSError as exc:
            raise TerminalError("pty.openpty failed: %s" % exc)
        # real initial window size on the pty (not just COLUMNS/LINES env)
        self._apply_winsize(master, cols, rows)
        try:
            proc = subprocess.Popen(
                [shell, "-l"],
                stdin=slave, stdout=slave, stderr=slave,
                env=env, start_new_session=True, close_fds=True,
            )
        except OSError as exc:
            os.close(master)
            os.close(slave)
            raise TerminalError("shell launch failed: %s" % exc)
        finally:
            # parent keeps only the master end
            try:
                os.close(slave)
            except OSError:
                pass
        stop = threading.Event()
        sess = {"master": master, "proc": proc, "stop": stop,
                "thread": None, "owner": owner}
        thread = threading.Thread(target=self._pump,
                                  args=(sid, send), daemon=True,
                                  name="terminal-pump-%d" % sid)
        sess["thread"] = thread
        with self._lock:
            self._sessions[sid] = sess
        thread.start()
        LOG.info("terminal session %d opened (pid %d)", sid, proc.pid)
        return sid

    def write(self, sid, raw: bytes):
        """Write client bytes into the pty. Raises TerminalError if unknown."""
        sess = self._get(sid)
        try:
            os.write(sess["master"], raw)
        except OSError as exc:
            raise TerminalError("pty write failed: %s" % exc)

    def resize(self, sid, cols, rows):
        """Apply a new pty window size via the TIOCSWINSZ ioctl (real).

        cols clamped to [20, 500], rows to [5, 200]. Raises TerminalError
        for an unknown session or a failed ioctl. Returns True on success.
        """
        cols = max(20, min(500, int(cols or 80)))
        rows = max(5, min(200, int(rows or 24)))
        sess = self._get(sid)
        if not self._apply_winsize(sess["master"], cols, rows):
            if TIOCSWINSZ is None:
                raise TerminalError("resize not supported on this platform")
            raise TerminalError("resize failed on session %r" % (sid,))
        LOG.debug("terminal session %d resized to %dx%d", sid, cols, rows)
        return True

    def close(self, sid):
        """Kill the session's process group and release its fds."""
        with self._lock:
            sess = self._sessions.pop(sid, None)
        if sess is None:
            return False
        sess["stop"].set()
        proc = sess["proc"]
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        try:
            proc.wait(timeout=3)
        except Exception:  # noqa: BLE001
            pass
        try:
            os.close(sess["master"])
        except OSError:
            pass
        thread = sess["thread"]
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3)
        LOG.info("terminal session %d closed", sid)
        return True

    def close_for_owner(self, owner):
        """Close every session owned by `owner` (e.g. on client disconnect)."""
        with self._lock:
            sids = [sid for sid, s in self._sessions.items() if s["owner"] is owner]
        for sid in sids:
            try:
                self.close(sid)
            except Exception:  # noqa: BLE001
                pass

    def count(self):
        with self._lock:
            return len(self._sessions)

    # -- internals --------------------------------------------------------------

    def _get(self, sid):
        with self._lock:
            sess = self._sessions.get(sid)
        if sess is None:
            raise TerminalError("unknown terminal session %r" % (sid,))
        return sess

    def _pump(self, sid, send):
        """Forward pty output -> TERMINAL_DATA until EOF or stop."""
        with self._lock:
            sess = self._sessions.get(sid)
        if sess is None:
            return
        master, stop = sess["master"], sess["stop"]
        try:
            while not stop.is_set():
                try:
                    r, _, _ = select.select([master], [], [], 0.5)
                except (OSError, ValueError):
                    break  # fd closed underneath us
                if not r:
                    continue
                try:
                    data = os.read(master, READ_CHUNK)
                except OSError:
                    break
                if not data:
                    break  # EOF: shell exited
                payload = json.dumps({
                    "session": sid,
                    "data": base64.b64encode(data).decode("ascii"),
                }).encode("utf-8")
                try:
                    send(proto.TERMINAL_DATA, payload)
                except OSError:
                    break
        finally:
            # notify the client the session is gone, then clean up locally
            try:
                send(proto.TERMINAL_CLOSE,
                     json.dumps({"session": sid}).encode("utf-8"))
            except OSError:
                pass
            try:
                self.close(sid)
            except Exception:  # noqa: BLE001
                pass
