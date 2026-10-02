#!/usr/bin/env python3
"""yourremote-settings: TUI settings editor for the Remote host.

Edits:
    /etc/remote/policy.conf   privacy/pairing/session/files toggles (INI)
    /etc/remote/apps.conf     allowlisted GUI apps for "open-app" (INI)
Shows:
    /etc/remote/device_id     this host's YR-XXXX-XXXX device id

Plain stdio numbered menus (robust over ssh, no curses needed). All input
goes through the validated parse_* helpers below, which are factored out
so they can be unit-tested. Writes are atomic (tmp file + rename).

Installed as /usr/bin/yourremote-settings. Needs write access to
/etc/remote (i.e. run with sudo).
"""
import configparser
import os
import re
import secrets
import shlex
import sys

CONFIG_DIR = os.environ.get("REMOTE_CONF_DIR", "/etc/remote")
POLICY_PATH = os.path.join(CONFIG_DIR, "policy.conf")
APPS_PATH = os.path.join(CONFIG_DIR, "apps.conf")
DEVICE_ID_PATH = os.path.join(CONFIG_DIR, "device_id")

# key -> (section, kind, prompt, default)
# kind: "bool" | "int:lo:hi" | "path-or-empty"
POLICY_SCHEMA = {
    "clipboard_sync": ("privacy", "bool",
                       "Sync text clipboard with viewers", "yes"),
    "clipboard_images": ("privacy", "bool",
                         "Sync clipboard images (v4 feature)", "no"),
    "audio_enabled": ("privacy", "bool",
                      "Audio streaming (planned)", "no"),
    "allow_new_pairing": ("pairing", "bool",
                          "Allow new devices to pair (trust-on-first-use)",
                          "yes"),
    "timeout_minutes": ("session", "int:0:1440",
                        "Session timeout, minutes (0 = never)", "30"),
    "file_root": ("files", "path-or-empty",
                  "File-transfer root (empty = service user's home)", ""),
}

DEVICE_ID_RE = re.compile(r"^YR-[A-Z0-9]{4}-[A-Z0-9]{4}$")
_APP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


# --------------------------------------------------------------------------
# Input validation (pure, testable)
# --------------------------------------------------------------------------
def parse_bool(text):
    """'yes/no', 'true/false', '1/0', 'on/off' (case-insensitive) -> bool."""
    t = text.strip().lower()
    if t in ("yes", "y", "true", "1", "on"):
        return True
    if t in ("no", "n", "false", "0", "off"):
        return False
    raise ValueError("expected yes/no, got %r" % text)


def parse_int(text, lo, hi):
    t = text.strip()
    if not re.fullmatch(r"-?\d+", t):
        raise ValueError("expected an integer, got %r" % text)
    v = int(t)
    if not (lo <= v <= hi):
        raise ValueError("expected %d..%d, got %d" % (lo, v, hi))
    return v


def parse_menu_choice(text, n):
    """1-based menu choice in 1..n -> int. Raises ValueError on bad input."""
    v = parse_int(text, 1, n)
    return v


def validate_exec_path(text):
    """App exec must be an absolute path. Returns the cleaned path."""
    t = text.strip()
    if not t.startswith("/"):
        raise ValueError("exec must be an absolute path, got %r" % text)
    if "\x00" in t:
        raise ValueError("exec contains NUL")
    return t


def validate_app_id(text):
    t = text.strip().lower()
    if not _APP_ID_RE.match(t):
        raise ValueError("app id must match [a-z0-9_-], got %r" % text)
    return t


def parse_policy_value(key, text):
    """Validate+normalize a policy.conf value per POLICY_SCHEMA."""
    section, kind, _prompt, _default = POLICY_SCHEMA[key]
    if kind == "bool":
        return "yes" if parse_bool(text) else "no"
    if kind.startswith("int:"):
        _, lo, hi = kind.split(":")
        return str(parse_int(text, int(lo), int(hi)))
    if kind == "path-or-empty":
        t = text.strip()
        if t == "":
            return ""
        return validate_exec_path(t)
    raise ValueError("unknown kind %r" % kind)


# --------------------------------------------------------------------------
# Config file I/O
# --------------------------------------------------------------------------
def load_ini(path):
    cfg = configparser.ConfigParser()
    cfg.optionxform = str  # keep key case as written
    if os.path.exists(path):
        cfg.read(path)
    return cfg


def save_ini(cfg, path):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        cfg.write(f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def get_policy(cfg, key):
    section, _kind, _prompt, default = POLICY_SCHEMA[key]
    try:
        return cfg.get(section, key)
    except (configparser.NoSectionError, configparser.NoOptionError):
        return default


def get_device_id(path=DEVICE_ID_PATH):
    """Return this host's device id, generating YR-XXXX-XXXX if missing."""
    try:
        with open(path) as f:
            did = f.read().strip()
        if DEVICE_ID_RE.match(did):
            return did
    except OSError:
        pass
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no ambiguous chars
    did = "YR-%s-%s" % ("".join(secrets.choice(alphabet) for _ in range(4)),
                        "".join(secrets.choice(alphabet) for _ in range(4)))
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(did + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return did


# --------------------------------------------------------------------------
# TUI
# --------------------------------------------------------------------------
def _prompt(text):
    try:
        return input(text)
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)


def _policy_menu(cfg):
    keys = list(POLICY_SCHEMA)
    while True:
        print("\n-- Policy toggles --")
        for i, k in enumerate(keys, 1):
            section, _kind, prompt, _d = POLICY_SCHEMA[k]
            print("  %d) [%s] %s = %s" % (i, section, prompt, get_policy(cfg, k)))
        print("  %d) back" % (len(keys) + 1))
        try:
            c = parse_menu_choice(_prompt("select> "), len(keys) + 1)
        except ValueError as e:
            print("invalid: %s" % e)
            continue
        if c == len(keys) + 1:
            return
        key = keys[c - 1]
        section, _kind, prompt, _d = POLICY_SCHEMA[key]
        cur = get_policy(cfg, key)
        raw = _prompt("  %s [%s]> " % (prompt, cur)).strip()
        if raw == "":
            continue
        try:
            new = parse_policy_value(key, raw)
        except ValueError as e:
            print("invalid: %s" % e)
            continue
        if not cfg.has_section(section):
            cfg.add_section(section)
        cfg.set(section, key, new)
        save_ini(cfg, POLICY_PATH)
        print("saved.")


def _apps_menu(cfg):
    while True:
        ids = cfg.sections()
        print("\n-- Allowed apps (for SYSTEM_CMD open-app) --")
        for i, aid in enumerate(ids, 1):
            name = cfg.get(aid, "name", fallback=aid)
            exe = cfg.get(aid, "exec", fallback="?")
            print("  %d) %s (%s): %s" % (i, aid, name, exe))
        print("  a) add app")
        print("  r) remove app")
        print("  b) back")
        raw = _prompt("select> ").strip().lower()
        if raw in ("b", "back", ""):
            return
        if raw == "a":
            try:
                aid = validate_app_id(_prompt("  app id (e.g. terminal)> "))
                name = _prompt("  display name> ").strip() or aid
                exe = validate_exec_path(_prompt("  exec absolute path> "))
                # sanity: argv-split the exec line (no shell is ever used)
                parts = shlex.split(exe)
                if not parts:
                    raise ValueError("empty exec")
            except ValueError as e:
                print("invalid: %s" % e)
                continue
            if not cfg.has_section(aid):
                cfg.add_section(aid)
            cfg.set(aid, "name", name)
            cfg.set(aid, "exec", exe)
            save_ini(cfg, APPS_PATH)
            print("saved.")
            continue
        if raw == "r":
            if not ids:
                print("no apps to remove.")
                continue
            try:
                c = parse_menu_choice(_prompt("  remove which?> "), len(ids))
            except ValueError as e:
                print("invalid: %s" % e)
                continue
            cfg.remove_section(ids[c - 1])
            save_ini(cfg, APPS_PATH)
            print("removed.")
            continue
        print("unknown option.")


def main():
    if not (os.access(CONFIG_DIR, os.W_OK) and os.access(CONFIG_DIR, os.X_OK)):
        print("ERROR: cannot write %s; re-run with sudo" % CONFIG_DIR,
              file=sys.stderr)
        return 1
    policy = load_ini(POLICY_PATH)
    apps = load_ini(APPS_PATH)
    while True:
        print("\n== Remote host settings ==")
        print("config dir : %s" % CONFIG_DIR)
        try:
            print("device id  : %s" % get_device_id())
        except OSError as e:
            print("device id  : (unavailable: %s)" % e)
        print("  1) policy toggles (privacy/pairing/session/files)")
        print("  2) allowed apps")
        print("  3) quit")
        try:
            c = parse_menu_choice(_prompt("select> "), 3)
        except ValueError as e:
            print("invalid: %s" % e)
            continue
        if c == 1:
            _policy_menu(policy)
        elif c == 2:
            _apps_menu(apps)
        else:
            return 0


if __name__ == "__main__":
    sys.exit(main())
