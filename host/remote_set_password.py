#!/usr/bin/env python3
"""remote-set-password: set the host access password for Remote.

Stores only a salt and a PBKDF2-derived key in /etc/remote/host.conf
(mode 0600) -- the password itself is never stored.

Non-interactive use: --password, or the REMOTE_PASSWORD environment
variable (preferred -- never appears in a process listing).
"""
import argparse
import base64
import getpass
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "common"))

import remote_proto as proto

DEFAULT_CONFIG = "/etc/remote/host.conf"


def set_password(password: str, path: str) -> None:
    if len(password) < 4:
        raise SystemExit("password must be at least 4 characters")
    salt = os.urandom(proto.SALT_LEN)
    key = proto.derive_key(password, salt)
    cfg = {"salt": base64.b64encode(salt).decode(),
           "key": base64.b64encode(key).decode(),
           "pbkdf2_iterations": proto.PBKDF2_ITERATIONS}
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    # write atomically, root-only readable
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    # The host service runs as the unprivileged `yourremote` user. When root
    # creates the real config, hand it to the service user — a root-owned
    # 0600 file would be unreadable to the daemon and it would never start.
    if path == DEFAULT_CONFIG and os.geteuid() == 0:
        try:
            import pwd
            pw = pwd.getpwnam("yourremote")
            os.chown(path, pw.pw_uid, pw.pw_gid)
        except KeyError:
            pass  # no service user on this machine (dev/test)
    print("password set in %s" % path)


def main():
    ap = argparse.ArgumentParser(description="set the Remote host password")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--password", help="non-interactive; also read from the "
                    "REMOTE_PASSWORD environment variable (preferred: never "
                    "appears on a command line)")
    args = ap.parse_args()
    if args.password is not None:
        pw = args.password
    elif os.environ.get("REMOTE_PASSWORD"):
        pw = os.environ["REMOTE_PASSWORD"]
    else:
        pw1 = getpass.getpass("New Remote password: ")
        pw2 = getpass.getpass("Confirm: ")
        if pw1 != pw2:
            raise SystemExit("passwords do not match")
        pw = pw1
    set_password(pw, args.config)


if __name__ == "__main__":
    main()
