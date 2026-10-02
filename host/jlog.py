#!/usr/bin/env python3
"""host/jlog.py -- JSON-lines structured logging with rotation for Remote.

Each record is one JSON object per line:

    {"ts": "2026-10-01T17:00:00.123456+00:00", "event": "connection", ...}

Event taxonomy (second positional arg / "event" field):
    connection          a viewer connected / disconnected
    auth                password / pairing auth attempt (success or failure)
    pairing             pairing-code issued / redeemed / revoked
    transfer            file transfer started / finished / failed
    command             SYSTEM_CMD executed (allowlisted action + result)
    error               unexpected exception in the host loop
    permission-change   PERMS_SET applied to a trusted device

Rotation: when a write would push the file past max_bytes, the current file
is renamed to <path>.1, <path>.1 -> <path>.2, ... up to <path>.<keep>;
the oldest generation is dropped. Pure stdlib, thread-safe.
"""
import datetime
import json
import os
import threading

DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_KEEP = 3


def _utcnow_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class JsonLogger:
    """Append-only JSON-lines logger with size-based rotation."""

    def __init__(self, path, max_bytes=DEFAULT_MAX_BYTES, keep=DEFAULT_KEEP):
        self.path = path
        self.max_bytes = int(max_bytes)
        self.keep = int(keep)
        if self.keep < 1:
            raise ValueError("keep must be >= 1")
        self._lock = threading.Lock()
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)

    def log(self, event, **fields):
        """Write one record. `event` names the event; extra fields are data."""
        record = {"ts": _utcnow_iso(), "event": event}
        record.update(fields)
        line = json.dumps(record, separators=(",", ":")) + "\n"
        data = line.encode("utf-8")
        with self._lock:
            self._maybe_rotate(len(data))
            with open(self.path, "ab") as f:
                f.write(data)

    def _maybe_rotate(self, incoming):
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return  # nothing to rotate yet
        if size + incoming <= self.max_bytes:
            return
        # drop the oldest generation, then shift the rest up by one
        oldest = "%s.%d" % (self.path, self.keep)
        try:
            os.remove(oldest)
        except OSError:
            pass
        for i in range(self.keep - 1, 0, -1):
            src = "%s.%d" % (self.path, i)
            dst = "%s.%d" % (self.path, i + 1)
            try:
                os.rename(src, dst)
            except OSError:
                pass
        try:
            os.rename(self.path, "%s.1" % self.path)
        except OSError:
            pass

    def read_all(self):
        """Return all records (current file only) as a list of dicts."""
        records = []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        records.append(json.loads(line))
        except OSError:
            pass
        return records
