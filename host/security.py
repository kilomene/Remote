#!/usr/bin/env python3
"""host/security.py -- service-user management and privilege handling.

The Remote host listens on TCP 47800 (well above 1024), so it never needs
root to bind. The systemd unit therefore runs the service directly as the
unprivileged `yourremote` system user (User=yourremote). No root startup,
no setuid dance at runtime.

`drop_privileges()` exists for the manual case: an admin starts
`remote-host` as root by hand and wants it to shed privileges before
serving. `ensure_user()` creates the system account (needs root); it also
has a dry_run mode that only reports what it WOULD do, for tests.
"""
import os
import pwd
import subprocess

SERVICE_USER = "yourremote"


class SecurityError(Exception):
    """Raised when a privilege operation cannot be performed honestly."""


def user_exists(username=SERVICE_USER):
    """True if the account exists (uses `id`, no parsing of /etc/passwd)."""
    try:
        proc = subprocess.run(["id", username],
                              capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def useradd_argv(username=SERVICE_USER):
    return ["useradd", "--system", "--no-create-home",
            "--shell", "/usr/sbin/nologin",
            "--comment", "Remote host service account", username]


def ensure_user(username=SERVICE_USER, dry_run=False):
    """Ensure the service account exists. Returns a list of action strings.

    dry_run=True performs no system changes; the returned strings describe
    what WOULD be done. Creating a user requires root.
    """
    actions = []
    if user_exists(username):
        actions.append("user %r already exists: nothing to do" % username)
        return actions
    if dry_run:
        actions.append("would create system user %r via: %s"
                       % (username, " ".join(useradd_argv(username))))
        actions.append("would set ownership of /etc/remote and "
                       "/var/log/remote to %r" % username)
        return actions
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        raise SecurityError(
            "creating user %r requires root; re-run with sudo" % username)
    proc = subprocess.run(useradd_argv(username),
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise SecurityError("useradd failed: %s" % (proc.stderr.strip()
                                                   or proc.stdout.strip()))
    actions.append("created system user %r" % username)
    return actions


def drop_privileges(username=SERVICE_USER):
    """Permanently drop from root to `username`. Irreversible in-process.

    Order: setgid first, then setuid (doing it the other way round loses
    the privilege needed for setgid). Clears supplementary groups.
    Raises SecurityError when not root (nothing to drop from) or when the
    target account does not exist.
    """
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        raise SecurityError("drop_privileges() requires root")
    try:
        pw = pwd.getpwnam(username)
    except KeyError:
        raise SecurityError("no such user: %r" % username)
    try:
        os.setgroups([])
    except OSError:
        pass
    os.setgid(pw.pw_gid)
    os.setuid(pw.pw_uid)
    # paranoia: prove the drop stuck
    if os.geteuid() == 0 or os.getuid() == 0:
        raise SecurityError("privilege drop failed: still root")
    os.environ.pop("SUDO_UID", None)
    os.environ.pop("SUDO_GID", None)
    return pw
