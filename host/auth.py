#!/usr/bin/env python3
"""Remote v4 host auth components: device identity, pairing, rate limiting,
session tokens, and the extended device store (12 permission flags).

Standalone module: no imports from remote_host.py (DeviceStore is copied and
extended here) and no import of remote_proto (v4 message numbers are quoted
literally in docstrings only).

Protocol v4 references:
  PAIR_REQUEST=0x77 (c->s JSON {code, device_id, device_name, platform}),
  PAIR_RESULT=0x78  (s->c JSON {ok, detail?}),
  PAIR_REQUIRED=0x79 (s->c JSON {device_id}),
  SESSION_TOKEN=0x85, SESSION_ROTATE=0x86 (s->c JSON {token, expires_in}).
"""
import datetime
import hmac
import json
import logging
import os
import secrets
import threading
import time

LOG = logging.getLogger("remote-auth")

# The twelve v4 permission flags (mirrors common/remote_proto.py, which is
# the canonical owner of the set).
PERMISSION_FLAGS = ("view", "mouse", "keyboard", "clipboard", "files",
                    "terminal", "system", "audio", "webcam", "apps",
                    "automation", "camera")

DEVICE_ID_LEN = 8  # hex chars after the "YR-" prefix, split as XXXX-XXXX

DEFAULT_DEVICE_ID_PATH = "/etc/remote/device-id"
DEFAULT_PAIRING_PATH = "/etc/remote/pairing_codes"
DEFAULT_DEVICES_PATH = "/etc/remote/devices.json"

PAIR_CODE_TTL = 10 * 60       # 10 minutes
PAIR_CODE_LEN = 6             # 6-digit decimal string
TOKEN_TTL = 3600              # 1 hour session token lifetime


# ---------------------------------------------------------------- device id

def get_or_create_device_id(path=DEFAULT_DEVICE_ID_PATH):
    """Return the host's permanent device id ("YR-XXXX-XXXX"), creating it
    once on first call. Uppercase hex from os.urandom, stored 0600."""
    try:
        with open(path, "r") as f:
            did = f.read().strip()
        if did and len(did) == 12 and did.startswith("YR-") \
                and all(c in "0123456789ABCDEF" for c in did[3:].replace("-", "")):
            return did
    except OSError:
        pass
    raw = os.urandom(DEVICE_ID_LEN // 2).hex().upper()  # 8 hex chars
    did = "YR-%s-%s" % (raw[:4], raw[4:])
    d = os.path.dirname(path)
    if d:
        try:
            os.makedirs(d, exist_ok=True)
        except OSError as exc:
            LOG.warning("cannot create dir for device id: %s", exc)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            f.write(did + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError as exc:
        LOG.warning("cannot persist device id: %s", exc)
    return did


# ---------------------------------------------------------------- pairing

class PairingManager:
    """Single-use pairing codes for PAIR_REQUEST (0x77) / PAIR_RESULT (0x78).

    Codes are 6-digit strings, expire after 10 minutes, and are consumed on
    first successful redeem. Persisted as JSON so a host restart does not
    invalidate outstanding codes. Code comparison is constant-time.
    """

    def __init__(self, path=DEFAULT_PAIRING_PATH):
        self.path = path
        self._lock = threading.Lock()

    def _read(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
            codes = data.get("codes", []) if isinstance(data, dict) else []
            return [c for c in codes if isinstance(c, dict) and "code" in c]
        except (OSError, ValueError):
            return []

    def _write(self, codes):
        tmp = self.path + ".tmp"
        d = os.path.dirname(self.path)
        if d:
            try:
                os.makedirs(d, exist_ok=True)
            except OSError:
                pass
        try:
            with open(tmp, "w") as f:
                json.dump({"codes": codes}, f, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
            return True
        except OSError as exc:
            LOG.warning("cannot persist pairing codes: %s", exc)
            return False

    def prune_expired(self):
        """Drop expired codes. Returns number of codes remaining."""
        now = time.time()
        with self._lock:
            codes = self._read()
            kept = [c for c in codes
                    if now - c.get("issued_at", 0) < PAIR_CODE_TTL]
            if len(kept) != len(codes):
                self._write(kept)
            return len(kept)

    def issue_code(self, device_name=""):
        """Issue a fresh single-use 6-digit code (never reuses a live one).

        Raises OSError if the code cannot be persisted — the caller must
        surface this instead of handing out a code that can never redeem.
        """
        with self._lock:
            codes = self._read()
            live = {c["code"] for c in codes
                    if time.time() - c.get("issued_at", 0) < PAIR_CODE_TTL}
            while True:
                code = "%06d" % secrets.randbelow(10 ** PAIR_CODE_LEN)
                if code not in live:
                    break
            codes.append({"code": code,
                          "issued_at": time.time(),
                          "device_name": device_name})
            if not self._write(codes):
                raise OSError("cannot write pairing store %s" % self.path)
            return code

    def redeem(self, code):
        """Redeem a code. Returns (ok: bool, detail: str).

        On success the code is consumed (single-use). Comparison is
        constant-time across stored live codes.
        """
        now = time.time()
        with self._lock:
            codes = self._read()
            live = [c for c in codes
                    if now - c.get("issued_at", 0) < PAIR_CODE_TTL]
            match = None
            for c in live:
                if hmac.compare_digest(str(c["code"]), str(code)):
                    match = c
                    break
            if match is None:
                # prune + report; also covers unknown / already-used / expired
                self._write(live)
                return False, "invalid or expired code"
            kept = [c for c in codes if c is not match]
            self._write(kept)
            return True, "paired"


# ------------------------------------------------------------ rate limiter

class RateLimiter:
    """Per-IP fail2ban-style backoff, in-process.

    After 5 consecutive failures from one IP, further attempts are refused
    for 60s, doubling per additional failure (120s, 240s, ...) up to a 900s
    cap. A success resets the counter for that IP.
    allow(ip) -> (allowed: bool, retry_after: float seconds).
    """

    BASE_FAILS = 5
    BASE_DELAY = 60.0
    MAX_DELAY = 900.0

    def __init__(self):
        self._lock = threading.Lock()
        self._state = {}  # ip -> {"fails": int, "last_fail": float}

    def record_failure(self, ip):
        with self._lock:
            st = self._state.setdefault(ip, {"fails": 0, "last_fail": 0.0})
            st["fails"] += 1
            st["last_fail"] = time.time()

    def record_success(self, ip):
        with self._lock:
            self._state.pop(ip, None)

    def _delay(self, fails):
        if fails < self.BASE_FAILS:
            return 0.0
        return min(self.BASE_DELAY * (2.0 ** (fails - self.BASE_FAILS)),
                   self.MAX_DELAY)

    def allow(self, ip):
        with self._lock:
            st = self._state.get(ip)
            if not st:
                return True, 0.0
            delay = self._delay(st["fails"])
            if delay <= 0:
                return True, 0.0
            wait = st["last_fail"] + delay - time.time()
            if wait <= 0:
                return True, 0.0
            return False, wait


# ----------------------------------------------------------- session tokens

class SessionTokens:
    """Session token nonces for SESSION_TOKEN (0x85) / SESSION_ROTATE (0x86).

    A token is bound to the live connection object that claimed it
    (validate(token, conn_id)) and expires after TOKEN_TTL (3600s). rotate()
    replaces a connection's token for the hourly SESSION_ROTATE, revoking the
    old one. Tokens are random 256-bit hex; lookups use constant-time compare.
    """

    def __init__(self, ttl=TOKEN_TTL):
        self.ttl = ttl
        self._lock = threading.Lock()
        self._tokens = {}  # token -> {"device_id", "conn_id", "expires_at"}

    def issue(self, device_id, conn_id):
        """Issue a token bound to conn_id. Returns (token, expires_in)."""
        token = secrets.token_hex(32)
        now = time.time()
        with self._lock:
            self._prune_locked(now)
            self._tokens[token] = {"device_id": device_id,
                                   "conn_id": conn_id,
                                   "expires_at": now + self.ttl,
                                   "issued_at": now}
        return token, self.ttl

    def _prune_locked(self, now):
        dead = [t for t, e in self._tokens.items()
                if e["expires_at"] <= now]
        for t in dead:
            del self._tokens[t]

    def prune(self):
        with self._lock:
            self._prune_locked(time.time())

    def validate(self, token, conn_id):
        """True iff token exists, is unexpired, and matches conn_id."""
        now = time.time()
        with self._lock:
            self._prune_locked(now)
            found = None
            for t, e in self._tokens.items():
                if hmac.compare_digest(t, str(token)):
                    found = (t, e)
                    break
            if found is None:
                return False
            _, e = found
            return e["conn_id"] == conn_id

    def device_for(self, token, conn_id):
        """device_id bound to a valid (token, conn_id), else None."""
        if not self.validate(token, conn_id):
            return None
        with self._lock:
            for t, e in self._tokens.items():
                if hmac.compare_digest(t, str(token)):
                    return e["device_id"]
        return None

    def rotate(self, device_id, conn_id):
        """Hourly rotation: revoke any token for conn_id, issue a fresh one."""
        with self._lock:
            old = [t for t, e in self._tokens.items()
                   if e["conn_id"] == conn_id]
            for t in old:
                del self._tokens[t]
        return self.issue(device_id, conn_id)

    def revoke(self, conn_id):
        """Drop all tokens for a connection (on disconnect)."""
        with self._lock:
            for t in [t for t, e in self._tokens.items()
                      if e["conn_id"] == conn_id]:
                del self._tokens[t]


# ------------------------------------------------------------ device store

class DeviceStore:
    """Trusted devices (trust-on-first-use via password/pairing). JSON at path.

    Copied from host/remote_host.py and extended for v4:
      * all twelve v4 PERMISSION_FLAGS (migration fills missing flags True)
      * temporary grants: grant(device_id, permissions, expires_at), with
        is_granted(device_id, feature=None) pruning expired grants
      * revoke(device_id): forget a device (and its grants)
      * block(device_id) / unblock / is_blocked: blocked devices are checked
        before trust is consulted

    File layout: {"devices": [...], "blocked": [device_id...],
                  "grants": {device_id: {"permissions": {...},
                                         "expires_at": ts}}}
    """

    def __init__(self, path=DEFAULT_DEVICES_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._devices = None
        self._blocked = None
        self._grants = None

    def _read_file(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return [], [], {}
            devs = data.get("devices", [])
            blocked = data.get("blocked", [])
            grants = data.get("grants", {})
            return ([d for d in devs if isinstance(d, dict)],
                    [b for b in blocked if isinstance(b, str)],
                    {k: v for k, v in grants.items() if isinstance(v, dict)})
        except (OSError, ValueError):
            return [], [], {}

    def _write_file(self, devices, blocked, grants):
        tmp = self.path + ".tmp"
        try:
            d = os.path.dirname(self.path)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(tmp, "w") as f:
                json.dump({"devices": devices, "blocked": blocked,
                           "grants": grants}, f, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError as exc:
            LOG.warning("cannot persist trusted devices: %s", exc)

    @staticmethod
    def _default_perms():
        return {f: True for f in PERMISSION_FLAGS}

    def _ensure_perms(self, entry):
        """Migrate an entry to a full twelve-flag boolean permissions dict."""
        perms = entry.get("permissions")
        if not isinstance(perms, dict):
            perms = {}
        for f in PERMISSION_FLAGS:
            if not isinstance(perms.get(f), bool):
                perms[f] = True
        entry["permissions"] = perms

    def _load_locked(self):
        if self._devices is None:
            devs, blocked, grants = self._read_file()
            for d in devs:
                self._ensure_perms(d)
            self._devices = devs
            self._blocked = blocked
            self._grants = grants
            self._write_file(self._devices, self._blocked, self._grants)

    def _save_locked(self):
        self._write_file(self._devices, self._blocked, self._grants)

    # -- trust / permissions (as in remote_host.py, v4 flag set) --

    def trust(self, device):
        """Record a device as trusted (idempotent). Returns True if new.
        Refuses blocked devices (returns False, does not trust)."""
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._lock:
            self._load_locked()
            did = device.get("device_id")
            if did in self._blocked:
                return False
            for d in self._devices:
                if d.get("device_id") == did:
                    d["last_seen"] = now
                    d["device_name"] = device.get("device_name",
                                                 d.get("device_name"))
                    d["platform"] = device.get("platform",
                                              d.get("platform"))
                    self._ensure_perms(d)
                    self._save_locked()
                    return False
            self._devices.append({
                "device_id": did if did else "unknown",
                "device_name": device.get("device_name", "unknown"),
                "platform": device.get("platform", "unknown"),
                "first_seen": now,
                "last_seen": now,
                "permissions": self._default_perms(),
            })
            self._save_locked()
            return True

    def set_permissions(self, device_id, perms):
        """Replace a device's permission flags. Returns False if unknown."""
        with self._lock:
            self._load_locked()
            for d in self._devices:
                if d.get("device_id") == device_id:
                    d["permissions"] = {f: bool(perms[f])
                                        for f in PERMISSION_FLAGS}
                    self._save_locked()
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

    # -- revocation / blocklist --

    def revoke(self, device_id):
        """Forget a device entirely (trust + temp grant). True if removed."""
        with self._lock:
            self._load_locked()
            before = len(self._devices)
            self._devices = [d for d in self._devices
                             if d.get("device_id") != device_id]
            self._grants.pop(device_id, None)
            self._save_locked()
            return len(self._devices) < before

    def block(self, device_id):
        """Add to the blocklist (checked before trust). Returns True if new."""
        with self._lock:
            self._load_locked()
            if device_id in self._blocked:
                return False
            self._blocked.append(device_id)
            self._save_locked()
            return True

    def unblock(self, device_id):
        """Remove from the blocklist. True if it was present."""
        with self._lock:
            self._load_locked()
            if device_id not in self._blocked:
                return False
            self._blocked.remove(device_id)
            self._save_locked()
            return True

    def is_blocked(self, device_id):
        with self._lock:
            self._load_locked()
            return device_id in self._blocked

    def is_trusted(self, device_id):
        """Trusted AND not blocked."""
        with self._lock:
            self._load_locked()
            if device_id in self._blocked:
                return False
            return any(d.get("device_id") == device_id
                       for d in self._devices)

    # -- temporary grants --

    def grant(self, device_id, permissions, expires_at):
        """Time-boxed permission override. permissions: dict of the twelve
        flags (missing flags default True); expires_at: unix timestamp."""
        with self._lock:
            self._load_locked()
            perms = {f: bool(permissions.get(f, True))
                     for f in PERMISSION_FLAGS} if permissions else \
                self._default_perms()
            self._grants[device_id] = {"permissions": perms,
                                       "expires_at": float(expires_at)}
            self._save_locked()

    def _prune_grants_locked(self):
        now = time.time()
        dead = [k for k, g in self._grants.items()
                if g.get("expires_at", 0) <= now]
        for k in dead:
            del self._grants[k]
        if dead:
            self._save_locked()

    def is_granted(self, device_id, feature=None):
        """Is a temp grant currently active? Prunes expired grants.

        With feature=None: True iff any live grant exists for the device.
        With feature set: True iff the live grant allows that flag.
        """
        with self._lock:
            self._load_locked()
            self._prune_grants_locked()
            g = self._grants.get(device_id)
            if not g:
                return False
            if feature is None:
                return True
            return bool(g["permissions"].get(feature, False))

    def list_grants(self):
        """Active grants (copies), expired ones pruned."""
        with self._lock:
            self._load_locked()
            self._prune_grants_locked()
            return {k: dict(v) for k, v in self._grants.items()}
