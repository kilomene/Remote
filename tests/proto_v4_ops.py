#!/usr/bin/env python3
"""tests/proto_v4_ops.py -- operations workstream checks for Remote v1.0.0.

Covers: jlog (JSON lines + rotation), updater (real parser on synthetic
GitHub payload + version compare), security.ensure_user(dry_run=True),
tray headless no-op, settings input validation, network wizard --help +
step functions, and a real build-deb.sh run asserting the new /usr/bin
symlinks and example configs land in the .deb.

Exit 0 and print "V4-OPS CHECKS PASSED" on success. No network access.
"""
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "host"))

import jlog
import security
import tray as tray_mod
import updater
import yourremote_network_setup as netsetup
import yourremote_settings as settings

FAILURES = []


def check(name, fn):
    try:
        fn()
    except Exception as e:
        FAILURES.append("%s: %s: %s" % (name, type(e).__name__, e))
        print("FAIL %s: %s" % (name, e))
    else:
        print("ok   %s" % name)


# ---------------------------------------------------------------- jlog
def t_jlog_lines_and_rotation():
    tmp = tempfile.mkdtemp(prefix="v4ops-jlog-")
    path = os.path.join(tmp, "remote.jsonl")
    lg = jlog.JsonLogger(path, max_bytes=300, keep=2)
    for i in range(6):
        lg.log("connection", device="dev%d" % i, ok=True, n=i)
    # every line of the current file is valid JSON with ts + event
    with open(path, encoding="utf-8") as f:
        lines = [ln for ln in f.read().splitlines() if ln.strip()]
    assert lines, "no lines written"
    for ln in lines:
        rec = json.loads(ln)
        assert "ts" in rec and rec["event"] == "connection", rec
    # rotation happened at tiny max_bytes, and keep=2 is respected
    assert os.path.exists(path + ".1"), "expected rotation to create .1"
    assert not os.path.exists(path + ".3"), "keep=2 violated: .3 exists"
    # event taxonomy writes work
    lg2 = jlog.JsonLogger(os.path.join(tmp, "other.jsonl"))
    lg2.log("auth", ok=False, reason="bad password")
    lg2.log("pairing", code_issued=True)
    lg2.log("transfer", bytes=123)
    lg2.log("command", cmd="lock", ok=True)
    lg2.log("error", where="loop")
    lg2.log("permission-change", device="d1", perms={"view": True})
    recs = lg2.read_all()
    assert [r["event"] for r in recs] == [
        "auth", "pairing", "transfer", "command", "error",
        "permission-change"], recs


# ---------------------------------------------------------------- updater
CANNED_RELEASE = {
    "tag_name": "v1.0.0",
    "name": "Remote v1.0.0",
    "published_at": "2026-10-01T00:00:00Z",
    "assets": [
        {"name": "remote_1.0.0_all.deb",
         "browser_download_url": "https://github.com/kilomene/Remote/releases/download/v1.0.0/remote_1.0.0_all.deb",
         "size": 123456},
        {"name": "remote-viewer.apk",
         "browser_download_url": "https://example.invalid/remote-viewer.apk",
         "size": 999},
    ],
}


def t_updater_parser_and_compare():
    # real parser, synthetic input
    rel = updater.parse_release_payload(CANNED_RELEASE)
    assert rel["version"] == "1.0.0", rel
    assert rel["tag"] == "v1.0.0", rel
    assert len(rel["assets"]) == 2, rel
    assert rel["assets"][0]["name"] == "remote_1.0.0_all.deb"
    assert rel["assets"][0]["size"] == 123456
    # real compare logic
    assert updater.compare_versions("0.3.0", "1.0.0") == -1
    assert updater.compare_versions("1.0.0", "1.0.0") == 0
    assert updater.compare_versions("v1.0.0", "1.0.0") == 0
    assert updater.compare_versions("2.0", "1.9.9") == 1
    assert updater.compare_versions("1.0.0", "1.0.0-rc1") == 1
    # check_update end-to-end with the network layer stubbed
    orig = updater.fetch_latest
    updater.fetch_latest = lambda timeout=15: rel
    try:
        res = updater.check_update(current="0.3.0")
    finally:
        updater.fetch_latest = orig
    assert res["current"] == "0.3.0", res
    assert res["latest"] == "1.0.0", res
    assert res["update_available"] is True, res
    assert res["assets"][0]["url"].startswith("https://github.com/"), res
    # asset-name gate used by download_and_install
    assert updater.ASSET_RE.match("remote_1.0.0_all.deb")
    assert not updater.ASSET_RE.match("remote-viewer.apk")
    assert not updater.ASSET_RE.match("../evil.deb")


# ---------------------------------------------------------------- security
def t_security_dry_run():
    before = security.user_exists("yourremote")
    actions = security.ensure_user("yourremote", dry_run=True)
    assert isinstance(actions, list) and actions, actions
    text = " ".join(actions)
    assert ("would create" in text and "useradd" in text) or \
        "already exists" in text, actions
    after = security.user_exists("yourremote")
    assert before == after, "dry_run must not change system state"
    # drop_privileges documents its root requirement honestly
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        try:
            security.drop_privileges("nobody")
        except security.SecurityError:
            pass
        else:
            raise AssertionError("drop_privileges should refuse non-root")


# ---------------------------------------------------------------- tray
def t_tray_headless_noop():
    for var in ("DISPLAY", "WAYLAND_DISPLAY"):
        os.environ.pop(var, None)
    tm = tray_mod.TrayManager()
    assert tm.available is False, "headless tray must report unavailable"
    # all of these must be clean no-ops, never raise
    tm.set_status(3, "tailscale")
    tm.notify("hello", "world")
    tm.run()
    tm.stop()


# ---------------------------------------------------------------- settings validation
def t_settings_validation():
    assert settings.parse_bool("yes") is True
    assert settings.parse_bool("OFF") is False
    assert settings.parse_bool("1") is True
    for bad in ("maybe", "", "2", "yess"):
        try:
            settings.parse_bool(bad)
        except ValueError:
            pass
        else:
            raise AssertionError("parse_bool(%r) should reject" % bad)
    assert settings.parse_int("30", 0, 1440) == 30
    for bad, lo, hi in (("abc", 0, 10), ("-1", 0, 10), ("11", 0, 10),
                        ("1.5", 0, 10)):
        try:
            settings.parse_int(bad, lo, hi)
        except ValueError:
            pass
        else:
            raise AssertionError("parse_int(%r) should reject" % bad)
    assert settings.parse_menu_choice("2", 3) == 2
    for bad in ("0", "4", "x", ""):
        try:
            settings.parse_menu_choice(bad, 3)
        except ValueError:
            pass
        else:
            raise AssertionError("parse_menu_choice(%r) should reject" % bad)
    assert settings.validate_exec_path("/usr/bin/xterm") == "/usr/bin/xterm"
    try:
        settings.validate_exec_path("relative/path")
    except ValueError:
        pass
    else:
        raise AssertionError("relative exec path should be rejected")
    assert settings.parse_policy_value("timeout_minutes", "45") == "45"
    assert settings.parse_policy_value("clipboard_sync", "no") == "no"
    try:
        settings.parse_policy_value("timeout_minutes", "99999")
    except ValueError:
        pass
    else:
        raise AssertionError("out-of-range timeout should be rejected")


# ---------------------------------------------------------------- network wizard
def t_network_wizard_help_and_steps():
    script = os.path.join(ROOT, "host", "yourremote_network_setup.py")
    proc = subprocess.run([sys.executable, script, "--help"],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert "--headscale" in proc.stdout, proc.stdout
    for fn in ("check_tailscale_installed", "install_tailscale",
               "tailscale_up", "verify_tailscale"):
        assert callable(getattr(netsetup, fn, None)), \
            "missing step function: %s" % fn
    # presence check is real (boolean, no exception)
    assert isinstance(netsetup.check_tailscale_installed(), bool)


def t_network_wizard_distro_flavor():
    # regression: a Debian 13 ('trixie') host must use the debian apt tree,
    # not ubuntu (live 404: pkgs.tailscale.com/stable/ubuntu/trixie.noarch.gpg)
    import tempfile
    cases = [
        ('ID=debian\nVERSION_CODENAME=trixie\n', "debian"),
        ('ID=ubuntu\nVERSION_CODENAME=noble\n', "ubuntu"),
        ('ID=linuxmint\nID_LIKE=ubuntu\n', "ubuntu"),
        ('ID=raspbian\nID_LIKE=debian\n', "debian"),
        ('ID=kali\nID_LIKE=debian\n', "debian"),
    ]
    for content, want in cases:
        with tempfile.NamedTemporaryFile("w", suffix=".os-release",
                                         delete=False) as f:
            f.write(content)
            path = f.name
        try:
            got = netsetup.distro_repo_flavor(path)
        finally:
            os.unlink(path)
        assert got == want, "os-release %r -> %r, want %r" % (content, got, want)
    # missing file: safe default (ubuntu tree covers most derivatives)
    assert netsetup.distro_repo_flavor("/nonexistent/os-release") == "ubuntu"


# ---------------------------------------------------------------- deb build
def t_deb_build_contents():
    build = os.path.join(ROOT, "packaging", "deb", "build-deb.sh")
    proc = subprocess.run(["bash", build], capture_output=True, text=True,
                          timeout=300, cwd=ROOT)
    assert proc.returncode == 0, "build-deb.sh failed:\n%s" % proc.stderr[-3000:]
    version = open(os.path.join(ROOT, "version.txt")).read().strip()
    deb = os.path.join(ROOT, "out", "remote_%s_all.deb" % version)
    assert os.path.exists(deb), "expected %s" % deb
    proc = subprocess.run(["dpkg-deb", "-c", deb], capture_output=True,
                          text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    listing = proc.stdout
    for link, target in (
            ("./usr/bin/yourremote-settings",
             "/opt/remote/lib/yourremote_settings.py"),
            ("./usr/bin/yourremote-network-setup",
             "/opt/remote/lib/yourremote_network_setup.py"),
            ("./usr/bin/yourremote-update",
             "/opt/remote/lib/yourremote_update.py"),
            ("./usr/bin/yourremote-pair-code",
             "/opt/remote/lib/yourremote_pair_code.py")):
        assert link in listing, "missing symlink entry %s" % link
        assert target in listing, "missing symlink target %s" % target
    for ex in ("policy.conf.example", "apps.conf.example",
               "commands.conf.example"):
        assert "./usr/share/doc/remote/examples/" + ex in listing, \
            "missing example %s" % ex
    assert "./opt/remote/version.txt" in listing, "missing version marker"
    proc = subprocess.run(["dpkg-deb", "-f", deb, "Recommends"],
                          capture_output=True, text=True, timeout=60)
    assert "tailscale" in proc.stdout, "Recommends lacks tailscale: %r" \
        % proc.stdout
    # maintainer scripts are executable shell
    p = subprocess.run(["dpkg-deb", "--ctrl-tarfile", deb],
                       capture_output=True, timeout=60)
    assert p.returncode == 0
    import tarfile, io
    tf = tarfile.open(fileobj=io.BytesIO(p.stdout))
    for scr in ("postinst", "prerm"):
        ti = tf.getmember("./" + scr)
        assert ti.mode & 0o111, "%s not executable" % scr


def main():
    check("jlog json lines + rotation", t_jlog_lines_and_rotation)
    check("updater parser + version compare", t_updater_parser_and_compare)
    check("security.ensure_user dry_run", t_security_dry_run)
    check("tray headless no-op", t_tray_headless_noop)
    check("settings input validation", t_settings_validation)
    check("network wizard --help + steps", t_network_wizard_help_and_steps)
    check("network wizard distro flavor", t_network_wizard_distro_flavor)
    check("deb build contents", t_deb_build_contents)
    if FAILURES:
        print("\n%d FAILURES" % len(FAILURES))
        for f in FAILURES:
            print(" - " + f)
        return 1
    print("V4-OPS CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
