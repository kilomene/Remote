#!/usr/bin/env bash
# End-to-end self-test for Remote v1. Run from the repo root: bash tests/selftest.sh
#  1. builds the .deb, checks its contents listing
#  2. checks the vendored wheels import
#  3. starts remote-host in --self-test on 127.0.0.1
#  4. runs the Linux viewer headless client (auth + frames + input + ping)
#  5. runs the independent Android-protocol conformance harness
#  6. asserts wrong-password auth fails
#  7. asserts the host accepted the input events
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PORT=47899
PASSWORD="selftest-secret-pw"
TMP="$(mktemp -d)"
HOST_LOG="$TMP/host.log"
HOST_PID=""

pass() { echo "PASS $1"; }
fail() { echo "FAIL $1"; exit 1; }

cleanup() {
    if [ -n "$HOST_PID" ]; then kill "$HOST_PID" 2>/dev/null || true; fi
    rm -rf "$TMP"
}
trap cleanup EXIT

echo "=== [1/7] build .deb ==="
bash packaging/deb/build-deb.sh
DEB="$(ls out/remote_*_all.deb | head -1)"
[ -f "$DEB" ] || fail "no .deb produced"
pass "built $DEB"

echo "=== [2/7] .deb contents ==="
LISTING="$(dpkg-deb -c "$DEB")"
for p in \
    "./usr/bin/remote-host" \
    "./usr/bin/remote-viewer" \
    "./usr/bin/remote-set-password" \
    "./opt/remote/lib/remote_host.py" \
    "./opt/remote/lib/remote_viewer.py" \
    "./opt/remote/lib/remote_set_password.py" \
    "./opt/remote/lib/remote_proto.py" \
    "./opt/remote/vendor/mss/__init__.py" \
    "./opt/remote/vendor/pynput/__init__.py" \
    "./lib/systemd/system/remote-host.service"; do
    echo "$LISTING" | grep -q "$p" || fail "missing in .deb: $p"
done
pass ".deb contains all expected paths"
dpkg-deb -f "$DEB" Package Version Depends | sed 's/^/  /'

echo "=== [3/7] vendored wheels import ==="
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

echo "=== [4/7] set password + start host (self-test) ==="
python3 host/remote_set_password.py --config "$TMP/host.conf" --password "$PASSWORD" >/dev/null
REMOTE_TEST_PASSWORD="$PASSWORD" REMOTE_VENDOR_DIR="$TMP/vendor" \
    python3 host/remote_host.py --self-test --bind 127.0.0.1 --port "$PORT" \
    >"$HOST_LOG" 2>&1 &
HOST_PID=$!
sleep 2
kill -0 "$HOST_PID" 2>/dev/null || { cat "$HOST_LOG"; fail "host died on startup"; }
grep -q "listening on" "$HOST_LOG" || { cat "$HOST_LOG"; fail "host not listening"; }
pass "host listening on 127.0.0.1:$PORT"

echo "=== [5/7] Linux viewer headless client ==="
python3 viewer/remote_viewer.py --self-test --host 127.0.0.1 --port "$PORT" \
    --password "$PASSWORD" --frames 20 | tee "$TMP/viewer.log"
grep -q "SELFTEST: PASS" "$TMP/viewer.log" || fail "viewer self-test did not pass"
pass "viewer: auth + 20 JPEG frames + input + ping/pong"

echo "=== [6/7] Android protocol conformance harness ==="
python3 tests/android_proto_check.py 127.0.0.1 "$PORT" "$PASSWORD" 10 \
    | tee "$TMP/proto.log"
grep -q "ALL PROTOCOL CHECKS PASSED" "$TMP/proto.log" \
    || fail "protocol harness failed"
pass "Android-shaped handshake/frames/input verified independently"

echo "=== [7/7] negative tests ==="
if python3 viewer/remote_viewer.py --self-test --host 127.0.0.1 --port "$PORT" \
        --password "wrong-password" --frames 2 >/dev/null 2>&1; then
    fail "wrong password was accepted"
fi
pass "wrong password rejected"
sleep 2  # let the host flush input logs
N_INPUTS="$(grep -c "self-test input accepted" "$HOST_LOG" || true)"
[ "$N_INPUTS" -ge 22 ] || { cat "$HOST_LOG"; fail "host accepted only $N_INPUTS input events (< 22)"; }
pass "host accepted $N_INPUTS input events (viewer 6 + harness 16)"

echo
echo "=============================="
echo "ALL SELF-TESTS PASSED"
echo "=============================="
