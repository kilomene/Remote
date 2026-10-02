"""host/automation.py -- EXEC_RUN (0x7A) allowlist executor for Remote v4.

The viewer sends EXEC_RUN {name, args:[]} and the host replies EXEC_RESULT
{name, ok, output, error?} (0x7B). Permission flag: "automation".

The admin allowlist lives in /etc/remote/commands.conf, a JSON object:

    {
      "disk-report": {
        "command": "/usr/bin/df",
        "args": "fixed",
        "fixed_args": ["-h", "/"]
      },
      "say": {
        "command": "/usr/bin/echo",
        "args": "free"
      }
    }

Rules (all enforced, no exceptions):
  * command must be an ABSOLUTE path to an executable file. Relative
    paths are dropped at load time.
  * "fixed"  -> caller args are IGNORED; fixed_args from the config run.
  * "free"   -> each caller arg must be a non-empty string, must NOT start
    with '-', and must NOT contain shell metacharacters. Rejected args fail
    the whole invocation with ok:false.
  * NEVER a raw shell: argv lists only, no shell=True anywhere.
  * Each run has a 30s timeout (config "timeout" may lower/raise it, 1..300).
  * stdout is captured (truncated to 16 KiB); on failure the error field
    carries stderr or the exit code.
  * Every invocation (allowed or rejected) is logged via logging.

Unknown command name -> {name, ok: False, error: "not allowed"}.
"""
import json
import logging
import os
import subprocess

LOG = logging.getLogger("remote-host")

TIMEOUT_S = 30
OUTPUT_LIMIT = 16384
ERROR_LIMIT = 4096

# Characters that are meaningful to a POSIX shell. Any "free" argument
# containing one is rejected outright (argv never reaches a shell anyway,
# but this keeps the allowlist conservative).
SHELL_METACHARS = set(';&|<>()$`"\'\\\n\r\t\0')


class AutomationEngine:
    """Executes admin-allowlisted commands. Unknown names are rejected."""

    def __init__(self, config="/etc/remote/commands.conf"):
        self.config_path = config
        self._cmds = self._load()

    # -- config -----------------------------------------------------------------

    def _load(self):
        try:
            with open(self.config_path) as f:
                data = json.load(f)
        except FileNotFoundError:
            LOG.warning("commands.conf %s missing: deny-all", self.config_path)
            return {}
        except (OSError, ValueError) as exc:
            LOG.warning("commands.conf %s unreadable (%s): deny-all",
                        self.config_path, exc)
            return {}
        if not isinstance(data, dict):
            LOG.warning("commands.conf %s is not a JSON object: deny-all",
                        self.config_path)
            return {}
        cmds = {}
        for name, entry in data.items():
            if not isinstance(name, str) or not name:
                continue
            if not isinstance(entry, dict):
                LOG.warning("commands.conf entry %r: not an object, skipped", name)
                continue
            command = entry.get("command")
            if (not isinstance(command, str) or not command
                    or not os.path.isabs(command)
                    or any(c in SHELL_METACHARS for c in command)):
                LOG.warning("commands.conf entry %r: command must be an "
                            "absolute path, skipped", name)
                continue
            if not os.access(command, os.X_OK):
                LOG.warning("commands.conf entry %r: %s not executable, skipped",
                            name, command)
                continue
            mode = entry.get("args", "fixed")
            if mode not in ("fixed", "free"):
                LOG.warning("commands.conf entry %r: args must be "
                            "'fixed' or 'free', skipped", name)
                continue
            fixed_args = entry.get("fixed_args", [])
            if not (isinstance(fixed_args, list) and
                    all(isinstance(a, str) for a in fixed_args)):
                LOG.warning("commands.conf entry %r: bad fixed_args, skipped", name)
                continue
            try:
                timeout = int(entry.get("timeout", TIMEOUT_S))
            except (TypeError, ValueError):
                timeout = TIMEOUT_S
            timeout = max(1, min(300, timeout))
            cmds[name] = {"command": command, "args": mode,
                          "fixed_args": list(fixed_args), "timeout": timeout}
        return cmds

    # -- API --------------------------------------------------------------------

    def list(self):
        """Sorted list of allowlisted command names."""
        return sorted(self._cmds)

    def run(self, name, args=None):
        """Run an allowlisted command.

        Returns {name, ok, output, error?} -- the EXEC_RESULT payload shape.
        """
        entry = self._cmds.get(name) if isinstance(name, str) else None
        if entry is None:
            LOG.warning("automation rejected: unknown command %r", name)
            return {"name": name, "ok": False, "error": "not allowed"}
        argv = [entry["command"]]
        if entry["args"] == "fixed":
            argv.extend(entry["fixed_args"])  # caller args ignored by design
        else:
            if args is None:
                args = []
            if not isinstance(args, list):
                LOG.warning("automation rejected: args not a list for %r", name)
                return {"name": name, "ok": False,
                        "error": "not allowed: args must be a list"}
            for a in args:
                if (not isinstance(a, str) or not a or a.startswith("-")
                        or any(c in SHELL_METACHARS for c in a)):
                    LOG.warning("automation rejected: bad arg %r for %r", a, name)
                    return {"name": name, "ok": False,
                            "error": "not allowed: bad argument"}
                argv.append(a)
        try:
            r = subprocess.run(argv, capture_output=True, text=True,
                               errors="replace", timeout=entry["timeout"])
        except subprocess.TimeoutExpired:
            LOG.error("automation %r timed out after %ds", name, entry["timeout"])
            return {"name": name, "ok": False,
                    "error": "timeout after %ds" % entry["timeout"]}
        except OSError as exc:
            LOG.error("automation %r exec failed: %s", name, exc)
            return {"name": name, "ok": False, "error": "exec failed: %s" % exc}
        ok = r.returncode == 0
        LOG.info("automation %r rc=%d ok=%s", name, r.returncode, ok)
        res = {"name": name, "ok": ok,
               "output": (r.stdout or "")[:OUTPUT_LIMIT]}
        if not ok:
            err = (r.stderr or "").strip()
            res["error"] = (err or "exit code %d" % r.returncode)[:ERROR_LIMIT]
        return res
