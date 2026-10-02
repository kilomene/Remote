#!/usr/bin/env python3
"""tests/proto_v4_sys.py -- protocol v4 system-stream conformance checks.

Covers the terminal/monitor/system/automation/apps/devtools workstream:
  * TERMINAL_RESIZE (0x69) on a real pty (resize + stty-size round-trip);
    invalid session id and invalid shell/env raise.
  * monitor.collect_status() returns all v4 keys with sane types; temps/gpu
    assert HONEST absence (empty list / None), never fake values.
  * sys_cmd: unknown cmd rejected; launch-app with empty apps.conf rejected
    with "not allowed"; service with a bogus name returns ok:false, no crash.
  * automation: unknown command rejected; arg sanitization rejects shell
    metachars and leading-dash args; a real allowlisted /bin/echo command
    returns real output.
  * devtools.detect_devtools() returns a dict (may be sparse on this box).

Exit 0 + "V4-SYS CHECKS PASSED" on success.
"""
import base64
import json
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "host"))
sys.path.insert(0, os.path.join(ROOT, "common"))

# Protocol v4 wire numbers (used literally; remote_proto.py is frozen).
TERMINAL_RESIZE = 0x69   # c->s JSON {session, cols, rows}
TERMINAL_DATA = 0x62     # s->c base64 pty bytes
EXEC_RUN = 0x7A          # c->s JSON {name, args:[]}
EXEC_RESULT = 0x7B       # s->c JSON {name, ok, output, error?}
assert (TERMINAL_RESIZE, EXEC_RUN, EXEC_RESULT) == (0x69, 0x7A, 0x7B)

from terminal import TerminalManager, TerminalError          # noqa: E402
from monitor import collect_status                             # noqa: E402
from sys_cmd import SysCmdExecutor                             # noqa: E402
from apps import AppLauncher                                  # noqa: E402
from automation import AutomationEngine                        # noqa: E402
from devtools import detect_devtools                           # noqa: E402


def check(cond, msg):
    if not cond:
        raise SystemExit("FAIL: " + msg)
    print("ok -", msg)


# -- terminal -----------------------------------------------------------------

tm = TerminalManager()
sent = []


def send(mtype, payload):
    sent.append((mtype, payload))


sid = tm.open(send, shell="/bin/bash", cols=80, rows=24,
              env={"REMOTE_V4_TEST": "yes"})
check(isinstance(sid, int), "terminal: opened real pty session")


def expect_stty(marker, what):
    """Type `stty size` and wait for the pty to report the marker bytes."""
    sent.clear()
    tm.write(sid, b"stty size\r")
    deadline = time.time() + 10
    while time.time() < deadline:
        for mtype, payload in sent:
            if mtype != TERMINAL_DATA:
                continue
            try:
                data = base64.b64decode(json.loads(payload)["data"])
            except (ValueError, KeyError):
                continue
            if marker in data:
                print("ok -", what)
                return
        time.sleep(0.1)
    raise SystemExit("FAIL: " + what)


expect_stty(b"24 80", "terminal: pty starts at requested geometry '24 80'")
check(tm.resize(sid, 100, 30) is True, "terminal: resize(100x30) ok")
expect_stty(b"30 100",
            "terminal: pty reports resized geometry '30 100' via stty size")
check(tm.resize(sid, 0, -5) is True, "terminal: out-of-range resize clamps")
expect_stty(b"5 80", "terminal: clamped resize reports '5 80' via stty size")

try:
    tm.resize(999999, 80, 24)
    check(False, "terminal: resize on invalid sid raises")
except TerminalError:
    print("ok - terminal: resize on invalid sid raises TerminalError")

try:
    tm.open(send, shell="/bin/evil-shell")
    check(False, "terminal: shell outside /etc/shells rejected")
except TerminalError:
    print("ok - terminal: shell outside /etc/shells rejected")

try:
    tm.open(send, env={"BAD KEY": "x"})
    check(False, "terminal: invalid env key rejected")
except TerminalError:
    print("ok - terminal: invalid env key rejected")

try:
    tm.open(send, env={"OK_KEY": 123})
    check(False, "terminal: non-string env value rejected")
except TerminalError:
    print("ok - terminal: non-string env value rejected")

tm.close(sid)

# -- monitor -------------------------------------------------------------------

st = collect_status([])
for key in ("cpu_pct", "mem_total_mb", "mem_used_mb", "disk_total_gb",
            "disk_used_gb", "net_rx_bps", "net_tx_bps", "services", "ts",
            "load_avg", "top_processes", "temperatures", "gpu",
            "uptime_s", "boot_ts"):
    check(key in st, "monitor: collect_status has %r" % key)
check(isinstance(st["cpu_pct"], float), "monitor: cpu_pct is float")
check(isinstance(st["load_avg"], list) and len(st["load_avg"]) == 3
      and all(isinstance(x, float) for x in st["load_avg"]),
      "monitor: load_avg is a 3-float list")
tp = st["top_processes"]
check(isinstance(tp, list) and len(tp) <= 8, "monitor: top_processes <= 8")
for p in tp:
    check(set(p) == {"pid", "name", "cpu_pct", "mem_mb"},
          "monitor: top_process entry shape")
    check(isinstance(p["pid"], int) and isinstance(p["name"], str)
          and isinstance(p["cpu_pct"], (int, float))
          and isinstance(p["mem_mb"], int),
          "monitor: top_process entry types")
check(isinstance(st["temperatures"], list), "monitor: temperatures is a list")
has_sensors = (os.path.isdir("/sys/class/hwmon")
               and any(os.listdir("/sys/class/hwmon"))) or \
              (os.path.isdir("/sys/class/thermal")
               and any(z.startswith("thermal_zone")
                       for z in os.listdir("/sys/class/thermal")))
if not has_sensors:
    check(st["temperatures"] == [],
          "monitor: temperatures honestly empty (no sensors exposed)")
if shutil.which("nvidia-smi") is None:
    check(st["gpu"] is None, "monitor: gpu honestly null (no nvidia-smi)")
else:
    check(set(st["gpu"]) == {"name", "util_pct", "temp_c",
                             "mem_used_mb", "mem_total_mb"},
          "monitor: gpu entry shape")
check(isinstance(st["uptime_s"], int) and st["uptime_s"] > 0,
      "monitor: uptime_s is a positive int")
check(isinstance(st["boot_ts"], int) and st["boot_ts"] > 0,
      "monitor: boot_ts is a positive int")

# -- sys_cmd -------------------------------------------------------------------

tmpd = tempfile.mkdtemp()

ex = SysCmdExecutor(dry_run=True)
r = ex.execute({"cmd": "bogus-cmd"})
check(r["ok"] is False and r["detail"] == "not allowed",
      "sys_cmd: unknown cmd rejected with 'not allowed'")

empty_conf = os.path.join(tmpd, "apps.conf")
with open(empty_conf, "w") as f:
    f.write("{}")
ex2 = SysCmdExecutor(dry_run=False,
                     apps_launcher=AppLauncher(config=empty_conf))
check(ex2.apps_launcher.list() == [], "apps: empty conf lists nothing")
r = ex2.execute({"cmd": "launch-app", "args": {"name": "firefox"}})
check(r["ok"] is False and "not allowed" in r["detail"],
      "sys_cmd: launch-app with empty apps.conf rejected ('not allowed')")

ex3 = SysCmdExecutor(dry_run=False,
                     monitored_services=["nonexistent-svc-xyz"])
r = ex3.execute({"cmd": "service",
                 "args": {"name": "nonexistent-svc-xyz", "action": "restart"}})
check(r["ok"] is False and isinstance(r["detail"], str) and r["detail"],
      "sys_cmd: service with bogus name returns ok:false (honest detail)")
r = ex3.execute({"cmd": "service", "args": {"name": "cron",
                                            "action": "restart"}})
check(r["ok"] is False and "not allowed" in r["detail"],
      "sys_cmd: service outside monitored list rejected")

# -- automation ----------------------------------------------------------------

cmds_conf = os.path.join(tmpd, "commands.conf")
with open(cmds_conf, "w") as f:
    json.dump({
        "echo-fixed": {"command": "/bin/echo", "args": "fixed",
                       "fixed_args": ["hello", "world"]},
        "echo-free": {"command": "/bin/echo", "args": "free"},
        "relative": {"command": "echo", "args": "free"},
    }, f)
eng = AutomationEngine(config=cmds_conf)
check(eng.list() == ["echo-fixed", "echo-free"],
      "automation: relative-path command dropped at load")

r = eng.run("nope", [])
check(r == {"name": "nope", "ok": False, "error": "not allowed"},
      "automation: unknown command rejected")

r = eng.run("echo-free", ['"; rm -rf /"'])
check(r["ok"] is False and "not allowed" in r["error"],
      "automation: shell-metachar arg rejected")
r = eng.run("echo-free", ["--evil"])
check(r["ok"] is False and "not allowed" in r["error"],
      "automation: leading-dash arg rejected")
r = eng.run("echo-free", ["a", "b|c"])
check(r["ok"] is False, "automation: pipe-char arg rejected")

r = eng.run("echo-fixed", ["IGNORED-BY-DESIGN"])
check(r["ok"] is True and "hello world" in r["output"]
      and "error" not in r,
      "automation: fixed command ignores caller args, real output")
check(set(r) <= {"name", "ok", "output", "error"},
      "automation: EXEC_RESULT payload shape {name, ok, output, error?}")

r = eng.run("echo-free", ["plain"])
check(r["ok"] is True and r["output"].strip() == "plain",
      "automation: free command runs real argv, real output")

# -- devtools ------------------------------------------------------------------

dt = detect_devtools()
check(isinstance(dt, dict), "devtools: returns a dict")
check("python3" in dt, "devtools: sees python3 on this box")
for tool, ver in dt.items():
    check(isinstance(tool, str) and (isinstance(ver, str) or ver is True),
          "devtools: {tool} -> version|True".format(tool=tool))
print("devtools found on this box:", sorted(dt))

print("V4-SYS CHECKS PASSED")
