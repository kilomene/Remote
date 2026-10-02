#!/usr/bin/env bash
# tests/test_install.sh — installer logic tests for install.sh.
#
# No docker in this environment, so a real install can't be tested here.
# Instead this harness shadows every privileged/external command with
# logging stubs and verifies: argument parsing, step ordering, idempotency
# (second run skips what's done), dry-run (nothing mutates), secret
# redaction (authkey/password never appear in installer output), the
# --version/--deb resolution, --uninstall, and the wizard's --authkey
# support. Manual full-install steps are documented at the bottom.
#
# Run from the repo root:  bash tests/test_install.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

pass() { echo "PASS $1"; }
fail() { echo "FAIL $1"; exit 1; }

TMP="$(mktemp -d)"
export TMP   # stubs are child processes; they need TMP for the flag file
trap 'rm -rf "$TMP"' EXIT
STUB_BIN="$TMP/stubs"; mkdir -p "$STUB_BIN"
export STUB_LOG="$TMP/invocations.log"
: > "$STUB_LOG"; rm -f "$TMP/stub_remote_version"

# --- stub commands -----------------------------------------------------------
mkstub() { # mkstub NAME BODY...
    local name="$1"; shift
    { echo "#!/usr/bin/env bash"; printf '%s\n' "$@"; } > "$STUB_BIN/$name"
    chmod +x "$STUB_BIN/$name"
}

mkstub apt-get         'echo "apt-get $*" >> "$STUB_LOG"
                        # simulate a real install: installing a .deb registers the package
                        for a in "$@"; do
                          case "$a" in *.deb) echo "9.9.9" > "$TMP/stub_remote_version";; esac
                        done
                        exit 0'
mkstub systemctl       'echo "systemctl $*" >> "$STUB_LOG"
                        if [ "${1:-}" = "is-active" ]; then
                          [ "${STUB_SERVICE_ACTIVE:-0}" = 1 ] && exit 0 || exit 3
                        fi; exit 0'
mkstub tailscale       'echo "tailscale $*" >> "$STUB_LOG"
                        if [ "${1:-}" = "ip" ]; then echo "${STUB_TAILSCALE_IP:-100.64.0.99}"; fi
                        exit 0'
mkstub yourremote-network-setup 'echo "yourremote-network-setup $*" >> "$STUB_LOG"; exit 0'
mkstub remote-set-password      'echo "remote-set-password argv=[$*] pw_env=$([ -n "${REMOTE_PASSWORD:-}" ] && echo SET || echo UNSET)" >> "$STUB_LOG"; exit 0'
mkstub curl            'echo "curl $*" >> "$STUB_LOG"
                        case "$*" in *api.github.com*) echo "{\"tag_name\": \"v9.9.9\"}";; esac
                        exit 0'
# dpkg: -s v4l2loopback-dkms succeeds iff STUB_V4L2=1; -i always "succeeds".
mkstub dpkg            'echo "dpkg $*" >> "$STUB_LOG"
                        if [ "${1:-}" = "-s" ] && [ "${2:-}" = "v4l2loopback-dkms" ] \
                           && [ "${STUB_V4L2:-0}" = 1 ]; then
                          echo "Status: install ok installed"; exit 0
                        fi
                        [ "${1:-}" = "-i" ] && exit 0
                        exit 1'
# dpkg-query: -W prints the installed remote version — the stub flag file
# (written by the apt-get stub on a simulated install), else
# $STUB_REMOTE_VERSION (empty = not installed); -s v4l2loopback-dkms
# succeeds iff STUB_V4L2=1.
mkstub dpkg-query      'echo "dpkg-query $*" >> "$STUB_LOG"
                        if [ "${1:-}" = "-W" ]; then
                          if [ -f "$TMP/stub_remote_version" ]; then cat "$TMP/stub_remote_version"
                          elif [ -n "${STUB_REMOTE_VERSION:-}" ]; then echo "$STUB_REMOTE_VERSION"
                          fi
                          exit 0
                        fi
                        if [ "${1:-}" = "-s" ] && [ "${2:-}" = "v4l2loopback-dkms" ] \
                           && [ "${STUB_V4L2:-0}" = 1 ]; then
                          echo "Status: install ok installed"; exit 0
                        fi
                        exit 1'

export PATH="$STUB_BIN:$PATH"

FAKE_DEB="$TMP/fake.deb"; : > "$FAKE_DEB"
SECRET_KEY="tskey-TESTSECRET-001"
SECRET_PW="test-password-001"

lineno_of() { grep -n -m1 "$1" "$STUB_LOG" | cut -d: -f1 || echo 99999; }

echo "=== [1] bash -n syntax ==="
bash -n install.sh || fail "bash -n install.sh"
pass "bash -n clean"

echo "=== [2] fresh headless install: step order + secrets redacted ==="
: > "$STUB_LOG"; rm -f "$TMP/stub_remote_version"
OUT="$TMP/out2.txt"
TS_AUTHKEY="$SECRET_KEY" REMOTE_PASSWORD="$SECRET_PW" \
    bash install.sh --yes --deb "$FAKE_DEB" --no-systemd >"$OUT" 2>&1 \
    || fail "fresh install exited non-zero (see $OUT)"

n_apt_deb=$(lineno_of "apt-get install -y $FAKE_DEB")
n_wizard=$(lineno_of "yourremote-network-setup --yes --authkey")
n_v4l2=$(lineno_of "apt-get install -y v4l2loopback-dkms")
n_pw=$(lineno_of "remote-set-password argv=\[\] pw_env=SET")
[ "$n_apt_deb" -lt 99999 ] || fail "deb install step missing"
[ "$n_wizard"  -lt 99999 ] || fail "network-setup step missing"
[ "$n_v4l2"    -lt 99999 ] || fail "v4l2loopback step missing"
[ "$n_pw"      -lt 99999 ] || fail "password step missing or used argv secret"
[ "$n_apt_deb" -lt "$n_wizard" ] || fail "deb must install before tailscale setup"
[ "$n_wizard" -lt "$n_v4l2" ]    || fail "tailscale must precede v4l2loopback"
[ "$n_v4l2" -lt "$n_pw" ]        || fail "v4l2loopback must precede password"
grep -q "systemctl" "$STUB_LOG" && fail "--no-systemd must skip systemctl"
grep -qF "$SECRET_KEY" "$OUT" && fail "authkey leaked into installer output"
grep -qF "$SECRET_PW" "$OUT" && fail "password leaked into installer output"
grep -q "authkey <redacted>" "$OUT" || fail "wizard invocation should show redacted authkey"
grep -q "100.64.0.99" "$OUT" || fail "summary missing tailscale IP"
pass "fresh install: order ok, secrets redacted, summary printed"

echo "=== [3] idempotent re-run skips completed steps ==="
: > "$STUB_LOG"; rm -f "$TMP/stub_remote_version"
export STUB_REMOTE_VERSION="9.9.9" STUB_V4L2=1
export REMOTE_HOST_CONF="$TMP/host.conf"; : > "$REMOTE_HOST_CONF"  # password "set"
OUT="$TMP/out3.txt"
TS_AUTHKEY="$SECRET_KEY" bash install.sh --yes --deb "$FAKE_DEB" --no-systemd >"$OUT" 2>&1 \
    || fail "re-run exited non-zero (see $OUT)"
grep -q "apt-get install" "$STUB_LOG" && fail "re-run must not reinstall anything"
grep -q "remote-set-password" "$STUB_LOG" && fail "re-run must not reset the password"
grep -q "already installed — skipping" "$OUT" || fail "expected skip message for .deb"
grep -q "keeping it" "$OUT" || fail "expected keep message for password"
grep -q "yourremote-network-setup" "$STUB_LOG" || fail "tailscale verify step should still run"
unset STUB_REMOTE_VERSION STUB_V4L2 REMOTE_HOST_CONF
pass "re-run is idempotent"

echo "=== [4] dry-run changes nothing ==="
: > "$STUB_LOG"; rm -f "$TMP/stub_remote_version"
OUT="$TMP/out4.txt"
TS_AUTHKEY="$SECRET_KEY" REMOTE_PASSWORD="$SECRET_PW" \
    bash install.sh --yes --deb "$FAKE_DEB" --no-systemd --dry-run >"$OUT" 2>&1 \
    || fail "dry-run exited non-zero (see $OUT)"
grep -q "apt-get install" "$STUB_LOG" && fail "dry-run executed a mutating apt-get"
grep -q "remote-set-password" "$STUB_LOG" && fail "dry-run executed remote-set-password"
grep -q "\[dry-run\]" "$OUT" || fail "dry-run output missing markers"
pass "dry-run is side-effect free"

echo "=== [5] --version pin + latest-tag resolution ==="
: > "$STUB_LOG"; rm -f "$TMP/stub_remote_version"
OUT="$TMP/out5.txt"
bash install.sh --yes --version 1.2.0 --dry-run --no-systemd >"$OUT" 2>&1 \
    || fail "version pin run failed (see $OUT)"
grep -q "remote_1.2.0_all.deb" "$OUT" || fail "pinned version URL wrong"
: > "$STUB_LOG"; rm -f "$TMP/stub_remote_version"
OUT="$TMP/out5b.txt"
bash install.sh --yes --dry-run --no-systemd >"$OUT" 2>&1 \
    || fail "latest-tag run failed (see $OUT)"
grep -q "remote_9.9.9_all.deb" "$OUT" || fail "latest tag (stubbed v9.9.9) not used"
grep -q "api.github.com" "$STUB_LOG" || fail "GitHub API not queried for latest"
pass "version resolution works"

echo "=== [6] --uninstall ==="
: > "$STUB_LOG"; rm -f "$TMP/stub_remote_version"
bash install.sh --uninstall --yes < /dev/null >/dev/null 2>&1 || fail "uninstall failed"
grep -q "apt-get purge -y remote" "$STUB_LOG" || fail "uninstall must purge the package"
pass "uninstall purges"

echo "=== [6b] --uninstall refuses on non-tty without --yes ==="
OUT="$TMP/out6b.txt"
bash install.sh --uninstall < /dev/null >"$OUT" 2>&1 && fail "uninstall without --yes on non-tty must fail"
grep -q "refusing" "$OUT" || fail "expected refusal message"
pass "uninstall refusal"

echo "=== [7] arg parsing errors ==="
bash install.sh --bogus-flag >/dev/null 2>&1 && fail "unknown flag must exit non-zero"
[ $? -eq 2 ] || fail "unknown flag must exit 2"
bash install.sh --help >/dev/null 2>&1 || fail "--help must exit 0"
pass "arg parsing"

echo "=== [8] wizard --authkey builds correct argv, redacts key ==="
python3 - <<'EOF'
import sys, io, contextlib
sys.path.insert(0, "host")
import yourremote_network_setup as w
calls = []
w.run = lambda argv, check=False, timeout=120: (calls.append(argv), (0, "", ""))[1]
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    assert w.tailscale_up(headscale_url="https://hs.example.com", authkey="tskey-SECRET") is None
out = buf.getvalue()
assert calls[0] == ["tailscale", "up", "--login-server", "https://hs.example.com",
                    "--authkey", "tskey-SECRET"], calls[0]
assert "tskey-SECRET" not in out, "authkey leaked: " + out
assert "<redacted>" in out
print("wizard authkey: OK")
EOF
pass "wizard authkey"

echo "=== [9] remote-set-password reads REMOTE_PASSWORD env, never stores plaintext ==="
PWCONF="$TMP/pw-host.conf"
REMOTE_PASSWORD="env-secret-456" python3 host/remote_set_password.py --config "$PWCONF" \
    || fail "remote-set-password with env failed"
[ "$(stat -c %a "$PWCONF")" = "600" ] || fail "host.conf must be 0600"
grep -q "env-secret-456" "$PWCONF" && fail "plaintext password stored in host.conf"
python3 - "$PWCONF" <<'EOF'
import json, sys, base64, os
sys.path.insert(0, os.path.join("host", "..", "common"))
sys.path.insert(0, "common")
import remote_proto as proto
cfg = json.load(open(sys.argv[1]))
key = proto.derive_key("env-secret-456", base64.b64decode(cfg["salt"]))
assert base64.b64encode(key).decode() == cfg["key"], "derived key mismatch"
print("env password derives correctly: OK")
EOF
pass "password env handling"

echo "=== [10] piped stdin (non-tty) auto-enables --yes ==="
: > "$STUB_LOG"; rm -f "$TMP/stub_remote_version"
OUT="$TMP/out10.txt"
# no --yes flag; stdin is /dev/null like a `curl ... | bash` pipe
TS_AUTHKEY="$SECRET_KEY" REMOTE_PASSWORD="$SECRET_PW" \
    bash install.sh --deb "$FAKE_DEB" --no-systemd < /dev/null >"$OUT" 2>&1 \
    || fail "piped install exited non-zero (see $OUT)"
grep -q "enabling --yes" "$OUT" || fail "expected auto --yes notice"
grep -q "yourremote-network-setup --yes --authkey" "$STUB_LOG" \
    || fail "wizard must be called with --yes on piped stdin"
pass "piped stdin auto --yes"

echo "=== [11] wizard survives EOF on the login prompt (piped stdin) ==="
python3 - <<'EOF'
import sys, io, contextlib
sys.path.insert(0, "host")
import yourremote_network_setup as w
calls = []
def fake_run(argv, check=False, timeout=120):
    calls.append(argv)
    if argv[:2] == ["tailscale", "up"]:
        return (0, "", "To authenticate, visit:\nhttps://login.tailscale.com/a/abc123\n")
    if argv[:2] == ["tailscale", "ip"]:
        return (0, "100.64.0.5\n", "")
    return (0, "ok\n", "")
w.run = fake_run
w.check_tailscale_installed = lambda: True
# simulate `input()` hitting EOF (piped stdin)
import builtins
real_input = builtins.input
builtins.input = lambda *a, **k: (_ for _ in ()).throw(EOFError())
sys.argv = ["yourremote-network-setup"]
try:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = w.main()
    out = buf.getvalue()
finally:
    builtins.input = real_input
assert rc == 0, "wizard must not crash on EOF; rc=%s out=%s" % (rc, out)
assert "100.64.0.5" in out, out
print("wizard EOF guard: OK")
EOF
pass "wizard EOF guard"

echo "=== [12] sudo re-exec preserves flags, promotes secrets to env ==="
: > "$STUB_LOG"; rm -f "$TMP/stub_remote_version"
mkstub id 'echo 1'   # not root
mkstub sudo 'echo "sudo argv=[$*]" >> "$STUB_LOG"
             echo "sudo env TS_AUTHKEY=${TS_AUTHKEY:+SET} REMOTE_PASSWORD=${REMOTE_PASSWORD:+SET}" >> "$STUB_LOG"
             exit 0'
OUT="$TMP/out12.txt"
bash install.sh --uninstall --yes --authkey "$SECRET_KEY" --password "$SECRET_PW" \
    < /dev/null >"$OUT" 2>&1 || fail "re-exec path failed (see $OUT)"
grep -q "sudo argv=.*--uninstall" "$STUB_LOG" || fail "re-exec lost --uninstall"
grep -q "sudo argv=.*--yes" "$STUB_LOG" || fail "re-exec lost --yes"
grep -q "sudo env TS_AUTHKEY=SET REMOTE_PASSWORD=SET" "$STUB_LOG" \
    || fail "secrets not promoted to env on re-exec"
grep -qF "$SECRET_KEY" "$STUB_LOG" && fail "authkey leaked into sudo argv"
grep -qF "$SECRET_PW" "$STUB_LOG" && fail "password leaked into sudo argv"
rm -f "$STUB_BIN/id" "$STUB_BIN/sudo"
pass "sudo re-exec"

echo "=== [13] stdout carries data only — progress chatter on stderr (regression) ==="
# A real agent hit this: resolve_deb()'s log/run chatter went to stdout, so
# deb="$(resolve_deb)" captured "== downloading...\n+ curl...\n/tmp/...deb"
# and apt-get received the blob instead of the path. Progress must be stderr.
: > "$STUB_LOG"; rm -f "$TMP/stub_remote_version"
OUT_STDOUT="$TMP/out13.txt"; OUT_STDERR="$TMP/err13.txt"
bash install.sh --yes --version 1.2.0 --dry-run --no-systemd \
    >"$OUT_STDOUT" 2>"$OUT_STDERR" \
    || fail "dry-run run failed"
# Note: the summary banner "================" is human output, not chatter —
# the regex requires the trailing-space form that log()/run() emit.
if grep -qE '^== |^\+ |^\[dry-run\]' "$OUT_STDOUT"; then
    fail "progress chatter leaked onto stdout (this broke apt-get's deb path)"
fi
grep -q "downloading" "$OUT_STDERR" \
    || fail "progress output missing from stderr (humans still need it)"
grep -q "Remote host ready" "$OUT_STDOUT" \
    || fail "summary missing from stdout"
pass "stdout clean; chatter on stderr"

echo
echo "==============================="
echo "ALL INSTALL TESTS PASSED"
echo "==============================="
echo
echo "MANUAL full-install test (no container runtime here to automate it):"
echo "  1. On a fresh Ubuntu 22.04/24.04 VM:"
echo "     curl -fsSL https://github.com/kilomene/Remote/releases/latest/download/install.sh -o /tmp/install-remote.sh"
echo "     sudo bash /tmp/install-remote.sh            # interactive: opens Tailscale auth URL"
echo "  2. Headless: sudo TS_AUTHKEY=tskey-... REMOTE_PASSWORD=... bash /tmp/install-remote.sh --yes"
echo "  3. Verify: systemctl is-active remote-host; tailscale ip -4; sudo yourremote-pair-code"
echo "  4. Pair the Android app to <tailscale-ip>:47800 and connect."
