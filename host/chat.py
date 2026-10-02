"""host/chat.py -- session chat log for Remote v3 (CHAT_MSG 0x64).

Each incoming chat message is appended as one JSON line:
{"from": ..., "text": ..., "ts": ...}.

Default path: /var/log/remote/chat.log. Overridable with the
REMOTE_CHAT_LOG environment variable (the self-test points it at a temp
file). Broadcast fan-out to the other sessions lives in HostServer
(broadcast_others); this module only owns the durable log.
"""
import json
import logging
import os
import threading
import time

LOG = logging.getLogger("remote-host")

DEFAULT_CHAT_LOG = "/var/log/remote/chat.log"


class ChatLog:
    def __init__(self, path=None):
        self.path = path or os.environ.get("REMOTE_CHAT_LOG") or DEFAULT_CHAT_LOG
        self._lock = threading.Lock()

    def append(self, sender, text, ts=None):
        line = json.dumps({
            "from": sender,
            "text": text,
            "ts": ts if ts is not None else time.time(),
        }) + "\n"
        with self._lock:
            try:
                d = os.path.dirname(self.path)
                if d:
                    os.makedirs(d, exist_ok=True)
                with open(self.path, "a") as f:
                    f.write(line)
            except OSError as exc:
                LOG.warning("cannot write chat log: %s", exc)
