"""host/sys_cmd.py -- SYSTEM_CMD (0x60) allowlist executor for Remote v4.

STRICT allowlist: the viewer may only trigger named system actions; there is
NEVER a raw shell. Anything not on the list is rejected with
{cmd, ok: False, detail: "not allowed"}.

v4: "service" runs `systemctl <action> <name>` synchronously and reports the
real outcome honestly (ok:false with detail when it fails, e.g. without
privileges); "launch-app" resolves names ONLY through host/apps.py
AppLauncher (empty apps.conf = deny-all). Permission flags: "apps" gates
launch-app, "automation" gates EXEC_RUN (host/automation.py), "system" gates
the rest.

All subprocess calls use argv lists (no shell=True anywhere), and launched
GUI apps run detached (Popen, start_new_session=True) so the host loop is
never blocked.

In self-test mode (dry_run=True) nothing is actually executed: after
allowlist + argument validation the executor returns
{ok: True, detail: "dry-run"} -- so the self-test never reboots the machine.
"""
import os
import shutil
import subprocess

DEVNULL = subprocess.DEVNULL


class SysCmdError(Exception):
    """Raised for argument validation / execution failures -> ok:false."""


# "open-app" strict map: friendly name -> desktop file(s) on the fixed allowlist.
APP_DESKTOPS = {
    "terminal": ["org.gnome.Terminal.desktop"],
    "files": ["org.gnome.Nautilus.desktop"],
    "browser": ["firefox.desktop", "google-chrome.desktop"],
    "vscode": ["code.desktop"],
}

# "open-terminal" candidates, first available wins.
TERMINAL_EMULATORS = ["x-terminal-emulator", "gnome-terminal", "konsole", "xterm"]


class SysCmdExecutor:
    """Executes validated system commands. dry_run=True skips execution."""

    def __init__(self, dry_run=False, displays_fn=None, monitored_services=None,
                 apps_launcher=None, pause_fn=None):
        self.dry_run = dry_run
        # displays_fn() -> {"displays": [{"id", ...}], "active": ...};
        # used to validate "switch-display" ids.
        self.displays_fn = displays_fn or (lambda: {"displays": [], "active": None})
        # "service" may only touch these units (from host.conf).
        self.monitored_services = list(monitored_services or [])
        # "launch-app" resolves ONLY through this AppLauncher (host/apps.py).
        # None -> created lazily on first use (keeps sys_cmd importable
        # even if apps.py is unavailable).
        self.apps_launcher = apps_launcher
        # "pause-screen" drives this (pause_fn(bool) -> str|None); the
        # integrator wires it to capture.paused.
        self.pause_fn = pause_fn
        self._cmds = {
            "lock": self._lock,
            "logout": self._logout,
            "reboot": self._reboot,
            "shutdown": self._shutdown,
            "suspend": self._suspend,
            "open-terminal": self._open_terminal,
            "open-browser": self._open_browser,
            "open-app": self._open_app,
            "launch-app": self._launch_app,
            "blank-screen": self._blank_screen,
            "switch-display": self._switch_display,
            "service": self._service,
            "pause-screen": self._pause_screen,
        }

    # -- entry point ----------------------------------------------------------

    def execute(self, req):
        """req: parsed JSON {cmd, args?}. Returns {cmd, ok, detail?}."""
        cmd = req.get("cmd") if isinstance(req, dict) else None
        if cmd not in self._cmds:
            return {"cmd": cmd, "ok": False, "detail": "not allowed"}
        args = req.get("args") or {}
        if not isinstance(args, dict):
            return {"cmd": cmd, "ok": False, "detail": "args must be an object"}
        try:
            detail = self._cmds[cmd](args)
        except SysCmdError as exc:
            return {"cmd": cmd, "ok": False, "detail": str(exc)}
        return {"cmd": cmd, "ok": True, "detail": detail}

    # -- helpers ---------------------------------------------------------------

    def _run(self, argv, check_tools=True):
        """Run a validated argv list detached. In dry-run: no-op."""
        if self.dry_run:
            return "dry-run"
        prog = argv[0]
        if check_tools and shutil.which(prog) is None:
            raise SysCmdError("tool not found: %s" % prog)
        try:
            subprocess.Popen(argv, stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL,
                             start_new_session=True)
        except OSError as exc:
            raise SysCmdError("launch failed: %s" % exc)
        return "launched: %s" % " ".join(argv)

    def _require_bool(self, args, key):
        val = args.get(key)
        if not isinstance(val, bool):
            raise SysCmdError("args.%s must be a boolean" % key)
        return val

    # -- commands ----------------------------------------------------------------

    def _lock(self, args):
        if self.dry_run:
            return "dry-run"
        if shutil.which("loginctl"):
            try:
                subprocess.Popen(["loginctl", "lock-session"],
                                 stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL,
                                 start_new_session=True)
                return "launched: loginctl lock-session"
            except OSError:
                pass
        return self._run(["xdg-screensaver", "lock"])

    def _logout(self, args):
        if self.dry_run:
            return "dry-run"
        user = os.environ.get("USER")
        if not user:
            raise SysCmdError("no user")
        return self._run(["loginctl", "terminate-user", user])

    def _reboot(self, args):
        return self._run(["systemctl", "reboot"])

    def _shutdown(self, args):
        return self._run(["systemctl", "poweroff"])

    def _suspend(self, args):
        return self._run(["systemctl", "suspend"])

    def _open_terminal(self, args):
        if self.dry_run:
            return "dry-run"
        for prog in TERMINAL_EMULATORS:
            if shutil.which(prog):
                return self._run([prog], check_tools=False)
        raise SysCmdError("no terminal emulator found")

    def _open_browser(self, args):
        url = args.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise SysCmdError("url must start with http:// or https://")
        return self._run(["xdg-open", url])

    def _open_app(self, args):
        name = args.get("name")
        desktops = APP_DESKTOPS.get(name) if isinstance(name, str) else None
        if not desktops:
            raise SysCmdError("not allowed: unknown app %r" % (name,))
        if self.dry_run:
            return "dry-run"
        desktop = next((d for d in desktops
                        if os.path.exists("/usr/share/applications/" + d)), None)
        if desktop is None:
            raise SysCmdError("app not installed: %s" % name)
        if shutil.which("gtk-launch"):
            return self._run(["gtk-launch", desktop], check_tools=False)
        if shutil.which("gio"):
            return self._run(["gio", "open",
                              "/usr/share/applications/" + desktop], check_tools=False)
        raise SysCmdError("no desktop launcher (gtk-launch/gio) available")

    def _blank_screen(self, args):
        on = self._require_bool(args, "on")
        return self._run(["xset", "dpms", "force", "off" if on else "on"])

    def _switch_display(self, args):
        disp_id = args.get("id")
        if not isinstance(disp_id, str) or not disp_id:
            raise SysCmdError("args.id must be a non-empty string")
        try:
            info = self.displays_fn()
        except Exception:  # noqa: BLE001 - displays provider must not break us
            info = {"displays": []}
        match = next((d for d in info.get("displays", [])
                      if d.get("id") == disp_id), None)
        if match is None:
            raise SysCmdError("unknown display id %r" % (disp_id,))
        name = match.get("name") or disp_id
        if self.dry_run:
            return "dry-run"
        return self._run(["xrandr", "--output", name, "--primary"])

    def _service(self, args):
        name = args.get("name")
        action = args.get("action")
        if not isinstance(name, str) or name not in self.monitored_services:
            raise SysCmdError("not allowed: unknown service %r" % (name,))
        if action not in ("start", "stop", "restart"):
            raise SysCmdError("not allowed: action must be start, stop or restart")
        if self.dry_run:
            return "dry-run"
        if shutil.which("systemctl") is None:
            raise SysCmdError("systemctl not available")
        # Synchronous: report the real outcome honestly. May fail without
        # privileges -- that surfaces as ok:false with the detail, not a crash.
        try:
            r = subprocess.run(["systemctl", action, name],
                               capture_output=True, text=True,
                               errors="replace", timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            raise SysCmdError("systemctl failed: %s" % exc)
        if r.returncode != 0:
            detail = ((r.stderr or "") + (r.stdout or "")).strip().splitlines()
            raise SysCmdError("systemctl %s %s failed (rc=%d)%s" % (
                action, name, r.returncode,
                (": " + detail[0][:200]) if detail else ""))
        return "systemctl %s %s ok" % (action, name)

    def _pause_screen(self, args):
        """pause-screen {paused: bool}: pause/resume frame streaming.

        Drives the integrator's pause_fn (capture.paused). Honest: without
        a wired pause_fn this is a no-op reported as such, never a fake
        "paused" state.
        """
        paused = self._require_bool(args, "paused")
        if self.dry_run:
            return "dry-run"
        if self.pause_fn is None:
            return "no capture wired (no-op)"
        try:
            self.pause_fn(bool(paused))
        except Exception as exc:  # noqa: BLE001
            raise SysCmdError("pause failed: %s" % exc)
        return "screen paused" if paused else "screen resumed"

    def _launch_app(self, args):
        """launch-app {name}: resolved ONLY via host/apps.py AppLauncher.

        Unknown name -> "not allowed". Empty/missing apps.conf is deny-all.
        Permission flag: "apps".
        """
        name = args.get("name")
        if not isinstance(name, str) or not name:
            raise SysCmdError("not allowed: args.name must be a non-empty string")
        launcher = self.apps_launcher
        if launcher is None:
            try:
                from apps import AppLauncher
            except ImportError as exc:
                raise SysCmdError("app launcher unavailable: %s" % exc)
            launcher = self.apps_launcher = AppLauncher()
        try:
            if self.dry_run:
                launcher.get(name)  # validate against the allowlist only
                return "dry-run"
            return launcher.launch(name)
        except Exception as exc:  # AppNotAllowed -> ok:false "not allowed"
            raise SysCmdError(str(exc))
