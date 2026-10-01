"""host/terminal.py -- remote terminal sessions for Remote v3.

TERMINAL_OPEN (0x61 c->s) opens a login shell on a fresh pty and replies
TERMINAL_OPENED (0x61 s->c) {session}. A pump thread per session forwards
pty output as TERMINAL_DATA (0x62, base64 of raw pty bytes); client input
arrives as TERMINAL_DATA and is written to the pty master. Either side may
send TERMINAL_CLOSE (0x63).

Concurrent sessions are capped at MAX_SESSIONS (8). The manager is
thread-safe; the send callback is invoked from pump threads (the host's
session.send is already send_lock-guarded).
"""
import base64
import json
import logging
import os
import pty
import select
import signal
import subprocess
import threading

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

    # -- lifecycle -------------------------------------------------------------

    def open(self, send, owner=None, cols=80, rows=24):
        """Start a login shell on a new pty. send(mtype, payload) is called
        from the pump thread. Returns the integer session id."""
        cols = max(20, min(500, int(cols or 80)))
        rows = max(5, min(200, int(rows or 24)))
        with self._lock:
            if len(self._sessions) >= MAX_SESSIONS:
                raise TerminalError("too many terminal sessions (max %d)" % MAX_SESSIONS)
            sid = self._next_id
            self._next_id += 1
        try:
            master, slave = pty.openpty()
        except OSError as exc:
            raise TerminalError("pty.openpty failed: %s" % exc)
        env = dict(os.environ)
        env["TERM"] = "xterm-256color"
        env["COLUMNS"] = str(cols)
        env["LINES"] = str(rows)
        shell = env.get("SHELL") or "/bin/bash"
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
