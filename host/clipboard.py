#!/usr/bin/env python3
"""Clipboard sync backend for remote-host (protocol v2/v3/v4).

Text + image/png support over X11 (xclip; xsel is text-only).

Wire shape (CLIPBOARD_SET = 0x40, JSON payload):
    text:   {"text": "..."}
    image:  {"mime": "image/png", "data": "<base64 PNG bytes>"}

Backends, in order of preference: xclip, xsel, in-memory (self-test /
headless), none. xsel cannot do image targets, so image support requires
xclip (or the memory backend). Images larger than MAX_IMAGE_BYTES are
skipped on broadcast (logged), never truncated.

Echo guard covers both kinds: text (or image bytes) applied from a
viewer is never re-broadcast when the monitor poll sees it.
"""
import base64
import hashlib
import json
import logging
import shutil
import subprocess
import threading

LOG = logging.getLogger("remote-host")

# Mirrors common/remote_proto.py (kept local so this module imports
# standalone without the protocol package on sys.path).
CLIPBOARD_SET = 0x40

MAX_IMAGE_BYTES = 2 * 1024 * 1024  # images larger than this are not broadcast


class ClipboardSync:
    """X11 clipboard monitor. Pushes local changes to viewers via the
    server broadcast; applies received text/images locally.

    Backends: xclip (text+image), xsel (text only), or in-memory
    (self-test / headless).
    """

    POLL_INTERVAL = 1.5

    def __init__(self, broadcast, memory=False):
        self._broadcast = broadcast
        self._lock = threading.Lock()
        self._last_remote_text = None   # text applied from a viewer (echo guard)
        self._last_remote_image = None  # sha256 of image bytes from a viewer
        self._last_seen_text = None
        self._last_seen_image = None    # sha256 digest
        self._backend = "memory" if memory else self._detect()
        self._mem = None  # ("text", str) | ("image", bytes) | None
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

    # -- reads -----------------------------------------------------------

    def get(self):
        """Current clipboard text, or None."""
        try:
            if self._backend == "memory":
                return self._mem[1] if self._mem and self._mem[0] == "text" else None
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

    def get_image(self):
        """Current clipboard image as raw PNG bytes, or None."""
        try:
            if self._backend == "memory":
                return self._mem[1] if self._mem and self._mem[0] == "image" else None
            if self._backend == "xclip":
                out = subprocess.run(
                    ["xclip", "-o", "-selection", "clipboard", "-t", "image/png"],
                    capture_output=True, timeout=5)
                if out.returncode != 0 or not out.stdout:
                    return None
                return bytes(out.stdout)
            # xsel has no mime-target support
            return None
        except (OSError, subprocess.SubprocessError):
            return None

    # -- writes ----------------------------------------------------------

    def set(self, text):
        """Apply text received from a viewer (marks it to avoid echo)."""
        with self._lock:
            self._last_remote_text = text
        try:
            if self._backend == "memory":
                self._mem = ("text", text)
            elif self._backend == "xclip":
                subprocess.run(["xclip", "-i", "-selection", "clipboard"],
                               input=text.encode("utf-8"), timeout=5, check=False)
            elif self._backend == "xsel":
                subprocess.run(["xsel", "--clipboard", "--input"],
                               input=text.encode("utf-8"), timeout=5, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            LOG.warning("clipboard set failed: %s", exc)

    def set_image(self, png_bytes):
        """Apply PNG image bytes received from a viewer (echo-guarded)."""
        digest = hashlib.sha256(png_bytes).hexdigest()
        with self._lock:
            self._last_remote_image = digest
        try:
            if self._backend == "memory":
                self._mem = ("image", bytes(png_bytes))
            elif self._backend == "xclip":
                subprocess.run(
                    ["xclip", "-i", "-selection", "clipboard", "-t", "image/png"],
                    input=bytes(png_bytes), timeout=10, check=False)
            elif self._backend == "xsel":
                LOG.warning("xsel backend cannot set images; image dropped")
        except (OSError, subprocess.SubprocessError) as exc:
            LOG.warning("clipboard image set failed: %s", exc)

    def set_from_wire(self, obj):
        """Apply a decoded CLIPBOARD_SET payload dict (text or image)."""
        if not isinstance(obj, dict):
            return
        if "text" in obj and isinstance(obj["text"], str):
            self.set(obj["text"])
        elif obj.get("mime") == "image/png" and isinstance(obj.get("data"), str):
            try:
                self.set_image(base64.b64decode(obj["data"], validate=True))
            except (ValueError, base64.binascii.Error) as exc:
                LOG.warning("bad clipboard image payload: %s", exc)

    # -- snapshots -------------------------------------------------------

    def current_text(self):
        return self.get()

    def current_payload(self):
        """Wire-shaped dict of the current clipboard, or None if empty."""
        img = self.get_image()
        if img is not None:
            if len(img) > MAX_IMAGE_BYTES:
                return None
            return {"mime": "image/png",
                    "data": base64.b64encode(img).decode("ascii")}
        text = self.get()
        if text is None:
            return None
        return {"text": text}

    # -- monitor ---------------------------------------------------------

    def _check_once(self):
        """One poll iteration. Returns a wire payload dict on a real
        (non-echo) change, else None. Image is checked before text."""
        if not self.enabled:
            return None
        img = self.get_image()
        if img is not None:
            if len(img) > MAX_IMAGE_BYTES:
                LOG.debug("clipboard image too large (%d bytes); skipped",
                          len(img))
                return None
            digest = hashlib.sha256(img).hexdigest()
            with self._lock:
                if digest in (self._last_seen_image, self._last_remote_image):
                    return None
                self._last_seen_image = digest
            return {"mime": "image/png",
                    "data": base64.b64encode(img).decode("ascii")}
        text = self.get()
        with self._lock:
            if text is None or text in (self._last_seen_text,
                                        self._last_remote_text):
                return None
            self._last_seen_text = text
        return {"text": text}

    def monitor_loop(self, stop):
        if not self.enabled:
            return
        while not stop.is_set():
            payload = self._check_once()
            if payload is not None:
                self._broadcast(CLIPBOARD_SET,
                                json.dumps(payload).encode("utf-8"))
                if "text" in payload:
                    LOG.debug("clipboard change broadcast (%d chars)",
                              len(payload["text"]))
                else:
                    LOG.debug("clipboard image broadcast (%d bytes b64)",
                              len(payload["data"]))
            stop.wait(self.POLL_INTERVAL)
