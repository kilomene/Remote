#!/usr/bin/env bash
# End-to-end self-test for Remote v3. Run from the repo root: bash tests/selftest.sh
#  1. builds the .deb, checks its contents listing (incl. v3 host modules)
#  2. checks the vendored wheels import
#  3. starts remote-host in --self-test on 127.0.0.1 (temp file root, trust db,
#     conn log, chat log)
#  4. runs the Linux viewer headless client (auth + frames + input + ping)
#  5. runs the independent protocol conformance harness (v1 + v2: pairing,
#     clipboard round-trip, file list/get/put/resume/mkdir/rename/delete,
#     traversal rejection, wrong-password rejection)
#  6. asserts wrong-password auth fails
#  7. asserts the host accepted the input events
#  8. asserts pairing persisted (trusted devices) and connection history logged
#  9. asserts file-transfer side effects on disk
# 10. runs the v3 conformance harness (system cmds incl. service, terminal
#     round-trip + cap, agent status, displays, chat relay + log, permission
#     enforcement, PERMS_SET/PERMS_LIST)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PORT=47899
PASSWORD="selftest-secret-pw"
TMP="$(mktemp -d)"
HOST_LOG="$TMP/host.log"
HOST_PID=""
export REMOTE_FILE_ROOT="$TMP/files"
export REMOTE_TRUSTED_FILE="$TMP/trusted"
export REMOTE_CONN_LOG="$TMP/conns.log"
export REMOTE_CHAT_LOG="$TMP/chat.log"

pass() { echo "PASS $1"; }
fail() { echo "FAIL $1"; exit 1; }

cleanup() {
    if [ -n "$HOST_PID" ]; then kill "$HOST_PID" 2>/dev/null || true; fi
    rm -rf "$TMP"
}
trap cleanup EXIT

echo "=== [1/10] build .deb ==="
bash packaging/deb/build-deb.sh
DEB="out/remote_$(cat version.txt | tr -d '[:space:]')_all.deb"
[ -f "$DEB" ] || fail "no .deb produced for version $(cat version.txt)"
# drop stale versioned artifacts so later globs can't pick them up
ls out/remote_*_all.deb 2>/dev/null | grep -v "$(basename "$DEB")" | xargs -r rm -f || true
[ -f "$DEB" ] || fail "no .deb produced"
pass "built $DEB"

echo "=== [2/10] .deb contents ==="
LISTING="$(dpkg-deb -c "$DEB")"
for p in \
    "./usr/bin/remote-host" \
    "./usr/bin/remote-viewer" \
    "./usr/bin/remote-set-password" \
    "./opt/remote/lib/remote_host.py" \
    "./opt/remote/lib/remote_viewer.py" \
    "./opt/remote/lib/remote_set_password.py" \
    "./opt/remote/lib/remote_proto.py" \
    "./opt/remote/lib/file_transfer.py" \
    "./opt/remote/lib/sys_cmd.py" \
    "./opt/remote/lib/terminal.py" \
    "./opt/remote/lib/agent_status.py" \
    "./opt/remote/lib/displays.py" \
    "./opt/remote/lib/chat.py" \
    "./opt/remote/vendor/mss/__init__.py" \
    "./opt/remote/vendor/pynput/__init__.py" \
    "./lib/systemd/system/remote-host.service"; do
    echo "$LISTING" | grep -q "$p" || fail "missing in .deb: $p"
done
pass ".deb contains all expected paths"
dpkg-deb -f "$DEB" Package Version Depends | sed 's/^/  /'

echo "=== [3/10] vendored wheels import ==="
mkdir -p "$TMP/vendor"
python3 -m zipfile -e packaging/vendor/mss-*.whl "$TMP/vendor/" >/dev/null
export TMPVENDOR="$TMP/vendor"
python3 - <<'EOF'
import os, sys
sys.path.insert(0, os.environ["TMPVENDOR"])
import mss
print("  mss", mss.__version__, "imports ok")
EOF
pass "vendored mss imports"

echo "=== [4/10] set password + start host (self-test) ==="
python3 host/remote_set_password.py --config "$TMP/host.conf" --password "$PASSWORD" >/dev/null
REMOTE_TEST_PASSWORD="$PASSWORD" REMOTE_VENDOR_DIR="$TMP/vendor" \
    python3 host/remote_host.py --self-test --bind 127.0.0.1 --port "$PORT" \
    >"$HOST_LOG" 2>&1 &
HOST_PID=$!
sleep 2
kill -0 "$HOST_PID" 2>/dev/null || { cat "$HOST_LOG"; fail "host died on startup"; }
grep -q "listening on" "$HOST_LOG" || { cat "$HOST_LOG"; fail "host not listening"; }
pass "host listening on 127.0.0.1:$PORT"

echo "=== [5/10] Linux viewer headless client ==="
python3 viewer/remote_viewer.py --self-test --host 127.0.0.1 --port "$PORT" \
    --password "$PASSWORD" --frames 20 | tee "$TMP/viewer.log"
grep -q "SELFTEST: PASS" "$TMP/viewer.log" || fail "viewer self-test did not pass"
pass "viewer: auth + 20 JPEG frames + input + ping/pong"

echo "=== [6/10] protocol conformance harness (v1+v2) ==="
python3 tests/android_proto_check.py 127.0.0.1 "$PORT" "$PASSWORD" 10 \
    | tee "$TMP/proto.log"
grep -q "ALL PROTOCOL CHECKS PASSED" "$TMP/proto.log" \
    || fail "protocol harness failed"
pass "handshake/frames/input/pairing/clipboard/file-transfer verified independently"

echo "=== [7/10] negative tests ==="
if python3 viewer/remote_viewer.py --self-test --host 127.0.0.1 --port "$PORT" \
        --password "wrong-password" --frames 2 >/dev/null 2>&1; then
    fail "wrong password was accepted"
fi
pass "wrong password rejected"
sleep 2  # let the host flush input logs
N_INPUTS="$(grep -c "self-test input accepted" "$HOST_LOG" || true)"
[ "$N_INPUTS" -ge 22 ] || { cat "$HOST_LOG"; fail "host accepted only $N_INPUTS input events (< 22)"; }
pass "host accepted $N_INPUTS input events (viewer 6 + harness 16)"

echo "=== [8/10] pairing persistence + connection history ==="
grep -q "harness-android-1" "$REMOTE_TRUSTED_FILE" \
    || fail "trusted devices missing harness-android-1"
grep -q "harness-android-2" "$REMOTE_TRUSTED_FILE" \
    || fail "trusted devices missing harness-android-2"
python3 - "$REMOTE_TRUSTED_FILE" <<'EOF'
import json, sys
d = json.load(open(sys.argv[1]))
assert isinstance(d.get("devices"), list) and len(d["devices"]) >= 2, "trust db malformed"
print("  trusted devices:", [(x["device_id"], x["platform"]) for x in d["devices"]])
EOF
grep -q "auth_ok harness-android-1" "$REMOTE_CONN_LOG" \
    || fail "connection log missing auth_ok for harness-android-1"
grep -q "auth_fail" "$REMOTE_CONN_LOG" \
    || fail "connection log missing auth_fail entry"
pass "pairing persisted; connection history logged"

echo "=== [9/10] file-transfer side effects on disk ==="
[ -d "$REMOTE_FILE_ROOT/harness/sub" ] || fail "uploaded dir missing on disk"
# up.bin was renamed to moved.bin then deleted; only the dir should remain
[ ! -e "$REMOTE_FILE_ROOT/harness/sub/moved.bin" ] || fail "deleted file still on disk"
# jail check: nothing escaped the file root
if find "$TMP" -maxdepth 1 -name "etc" -o -maxdepth 1 -name "passwd" | grep -q .; then
    fail "path traversal escaped the jail"
fi
pass "file ops landed inside the jail; traversal contained"

echo "=== [10/10] protocol v3 conformance harness ==="
python3 tests/proto_v3_check.py 127.0.0.1 "$PORT" "$PASSWORD" \
    | tee "$TMP/protov3.log"
grep -q "V3 CHECKS PASSED" "$TMP/protov3.log" \
    || fail "v3 protocol harness failed"
# chat was really persisted (no-stub audit: broadcast + disk)
grep -q "chat-hello-v3" "$REMOTE_CHAT_LOG" \
    || fail "chat log missing the relayed message"
# permissions were really persisted per device
python3 - "$REMOTE_TRUSTED_FILE" <<'EOF'
import json, sys
devs = {d["device_id"]: d for d in json.load(open(sys.argv[1]))["devices"]}
for did in ("harness-v3-1", "harness-v3-2"):
    assert did in devs, "missing trusted device %s" % did
    perms = devs[did].get("permissions")
    assert isinstance(perms, dict) and set(perms) == {
        "view", "mouse", "keyboard", "clipboard", "files", "terminal", "system"
    } and all(isinstance(v, bool) for v in perms.values()), \
        "bad permissions for %s: %r" % (did, perms)
print("  permissions persisted for:", sorted(devs))
EOF
pass "v3: system cmds, terminal, agent status, displays, chat, permissions verified"

echo
echo "=============================="
echo "ALL SELF-TESTS PASSED"
echo "=============================="
