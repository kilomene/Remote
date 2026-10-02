#!/usr/bin/env python3
"""Host privacy policy toggles: /etc/remote/policy.conf (JSON).

Toggle keys:
  require_approval    new sessions need the local user's approval
  require_pairing      viewers must complete PAIR_REQUEST pairing first
  allow_files         file transfer allowed
  allow_terminal      remote terminal allowed
  allow_clipboard     clipboard sync allowed
  allow_audio         remote audio capture allowed
  allow_webcam        webcam capture allowed
  hide_on_connect     hide the tray/UI indicator while connected
  idle_disconnect_min drop idle sessions after N minutes (0 = never)

Defaults are all-allow (everything permitted) except require_approval=false.
load() is tolerant: missing or corrupt file -> defaults.
"""
import json
import logging
import os

LOG = logging.getLogger("remote-policy")

DEFAULT_PATH = "/etc/remote/policy.conf"

DEFAULTS = {
    "require_approval": False,
    "require_pairing": False,
    "allow_files": True,
    "allow_terminal": True,
    "allow_clipboard": True,
    "allow_audio": True,
    "allow_webcam": True,
    "hide_on_connect": False,
    "idle_disconnect_min": 0,
}

# feature name -> policy toggle key
_FEATURE_MAP = {
    "files": "allow_files",
    "terminal": "allow_terminal",
    "clipboard": "allow_clipboard",
    "audio": "allow_audio",
    "webcam": "allow_webcam",
    "approval": "require_approval",
    "pairing": "require_pairing",
}


class Policy:
    """Privacy toggles. check(feature) answers enforcement questions.

    Features: files, terminal, clipboard, audio, webcam, approval, pairing.
    Unknown features fail closed (False).
    """

    def __init__(self, path=DEFAULT_PATH):
        self.path = path
        self._toggles = dict(DEFAULTS)
        self.load()

    def load(self):
        """(Re)load from disk; missing/corrupt file keeps defaults."""
        try:
            with open(self.path) as f:
                data = json.load(f)
        except (OSError, ValueError) as exc:
            if not isinstance(exc, FileNotFoundError):
                LOG.warning("policy file unreadable (%s): using defaults",
                            self.path)
            return
        if not isinstance(data, dict):
            LOG.warning("policy file malformed: using defaults")
            return
        for key in DEFAULTS:
            if key in data and isinstance(data[key], type(DEFAULTS[key])):
                self._toggles[key] = data[key]
            elif key in data:
                LOG.warning("policy key %r wrong type: keeping default", key)

    def save(self):
        """Persist current toggles (0600)."""
        tmp = self.path + ".tmp"
        try:
            d = os.path.dirname(self.path)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(tmp, "w") as f:
                json.dump(self._toggles, f, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError as exc:
            LOG.warning("cannot persist policy: %s", exc)

    def get(self, key):
        return self._toggles.get(key, DEFAULTS.get(key))

    def set(self, key, value):
        if key not in DEFAULTS:
            raise KeyError("unknown policy key: %r" % key)
        if not isinstance(value, type(DEFAULTS[key])):
            raise TypeError("policy key %r expects %s" %
                            (key, type(DEFAULTS[key]).__name__))
        self._toggles[key] = value

    def check(self, feature):
        """Enforcement query. True iff feature is allowed by policy."""
        key = _FEATURE_MAP.get(feature)
        if key is None:
            return False  # fail closed on unknown features
        return bool(self._toggles.get(key, False))

    def as_dict(self):
        return dict(self._toggles)
