"""host/apps.py -- admin-configured GUI app launcher for Remote v4.

Used ONLY by the "launch-app" SYSTEM_CMD (permission flag "apps"): the host
never launches anything that is not explicitly listed in the config file.

Config file (default /etc/remote/apps.conf) is a JSON object mapping a
friendly name to an argv list:

    {
      "firefox":  {"command": ["/usr/bin/firefox"], "icon": "firefox"},
      "files":   {"command": ["/usr/bin/nautilus", "--new-window"]}
    }

Empty, missing, or invalid config -> DENY-ALL: every launch() raises
AppNotAllowed ("not allowed"). The admin MUST populate the file; there is
no default set of launchable apps.

Launches use argv lists only -- never a shell. The child is detached
(start_new_session=True) so the host loop is never blocked.
"""
import json
import logging
import subprocess

LOG = logging.getLogger("remote-host")

DEVNULL = subprocess.DEVNULL


class AppNotAllowed(Exception):
    """Raised when an app name is not on the allowlist or can't launch."""


class AppLauncher:
    """Resolves friendly app names to validated argv lists and launches them."""

    def __init__(self, config="/etc/remote/apps.conf"):
        self.config_path = config
        self._apps = self._load()

    # -- config -----------------------------------------------------------------

    def _load(self):
        try:
            with open(self.config_path) as f:
                data = json.load(f)
        except FileNotFoundError:
            LOG.warning("apps.conf %s missing: deny-all (admin must populate it)",
                        self.config_path)
            return {}
        except (OSError, ValueError) as exc:
            LOG.warning("apps.conf %s unreadable (%s): deny-all",
                        self.config_path, exc)
            return {}
        if not isinstance(data, dict):
            LOG.warning("apps.conf %s is not a JSON object: deny-all",
                        self.config_path)
            return {}
        apps = {}
        for name, entry in data.items():
            if not isinstance(name, str) or not name:
                continue
            if not isinstance(entry, dict):
                LOG.warning("apps.conf entry %r: not an object, skipped", name)
                continue
            argv = entry.get("command")
            if not (isinstance(argv, list) and argv and
                    all(isinstance(a, str) and a for a in argv)):
                LOG.warning("apps.conf entry %r: bad command argv, skipped", name)
                continue
            apps[name] = {"command": list(argv), "icon": entry.get("icon")}
        return apps

    # -- API --------------------------------------------------------------------

    def list(self):
        """Sorted list of configured app names."""
        return sorted(self._apps)

    def get(self, name):
        """Return the entry dict for `name`; raise AppNotAllowed if unknown."""
        entry = self._apps.get(name) if isinstance(name, str) else None
        if entry is None:
            raise AppNotAllowed("not allowed: unknown app %r" % (name,))
        return entry

    def launch(self, name):
        """Launch the configured argv detached. Returns a detail string.

        Raises AppNotAllowed for unknown names or spawn failures.
        """
        entry = self.get(name)
        argv = entry["command"]
        try:
            subprocess.Popen(argv, stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL,
                             start_new_session=True, close_fds=True)
        except OSError as exc:
            raise AppNotAllowed("launch failed: %s" % exc)
        LOG.info("launched app %r: %s", name, " ".join(argv))
        return "launched: %s" % " ".join(argv)
