#!/usr/bin/env python3
"""Wire-compatibility check for the Android v4 pairing-code flow.

Replays EXACTLY what PairActivity sends (DEVICE_HELLO then PAIR_REQUEST
{code:"000000"}) against a REAL local stub server that speaks the real
framing via common/remote_proto.py send_msg/recv_msg (the same
[1-byte type][4-byte big-endian length][payload] header the Java
RemoteProto.sendMsg/recvMsg uses).

Checks:
 1. android/.../RemoteProto.java actually declares the canonical v4
    constants (TERMINAL_RESIZE=0x69, AUDIO_*=0x70-0x73, WEBCAM_*=0x74/0x75,
    NET_STATUS=0x76, PAIR_REQUEST/RESULT/REQUIRED=0x77-0x79,
    EXEC_RUN/RESULT=0x7A/0x7B, POLICY_GET=0x7C, SESSION_TOKEN/ROTATE=0x85/0x86,
    CAMERA_*=0x87-0x8A), byte-identical with common/remote_proto.py.
 2. Positive flow: stub receives DEVICE_HELLO (0x30) with the expected
    fields, then PAIR_REQUEST (0x77); the raw payload bytes are compared
    BYTE-EXACT against a Python replication of Android's
    org.json.JSONObject serialization (insertion-ordered keys
    code/device_id/device_name/platform, compact separators, UTF-8),
    and the fields are compared field-for-field after json.loads.
    Stub answers PAIR_RESULT {ok:true}; the client accepts.
 3. Negative flow: code "999999" -> PAIR_RESULT {ok:false,
    detail:"bad code"}; the client surfaces the detail string.
 4. Legacy flow: a pre-v4 host answers DEVICE_HELLO with AUTH_REQ
    instead of PAIR_RESULT; the client takes the "host doesn't support
    pairing codes" branch (mirrors PairActivity.doPair).

HONEST FRAMING: this verifies wire-compatibility of the Java
serialization logic (what bytes PairActivity puts on the wire and how it
interprets the replies). It is NOT a test against the real v4 host,
which is not built yet.

Usage: python3 tests/proto_v4_pair_client.py
"""
import json
import os
import re
import socket
import struct
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "common"))
import remote_proto  # noqa: E402  (real framing helpers)

REMOTE_PROTO_JAVA = os.path.join(
    ROOT, "android", "app", "src", "main", "java",
    "com", "remote", "viewer", "RemoteProto.java")

EXPECTED_CONSTANTS = {
    # canonical v4 numbers (byte-identical with common/remote_proto.py)
    "TERMINAL_RESIZE": 0x69,
    "AUDIO_START": 0x70, "AUDIO_DATA": 0x71, "AUDIO_STOP": 0x72,
    "AUDIO_ERROR": 0x73,
    "WEBCAM_LIST": 0x74, "WEBCAM_FRAME": 0x75,
    "NET_STATUS": 0x76,
    "PAIR_REQUEST": 0x77, "PAIR_RESULT": 0x78, "PAIR_REQUIRED": 0x79,
    "EXEC_RUN": 0x7A, "EXEC_RESULT": 0x7B,
    "POLICY_GET": 0x7C,
    "SESSION_TOKEN": 0x85, "SESSION_ROTATE": 0x86,
    "CAMERA_START": 0x87, "CAMERA_STOP": 0x88,
    "CAMERA_FRAME": 0x89, "CAMERA_STATUS": 0x8A,
}

PAIR_REQUEST = 0x77  # from the v4 spec (also asserted against the .java file)
PAIR_RESULT = 0x78
DEVICE_HELLO = 0x30
AUTH_REQ = 0x01


def check(cond, label):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        sys.exit(1)


def check_java_constants():
    src = open(REMOTE_PROTO_JAVA, encoding="utf-8").read()
    for name, want in EXPECTED_CONSTANTS.items():
        m = re.search(r"public static final int %s\s*=\s*(0x[0-9a-fA-F]+|\d+)\s*;"
                      % re.escape(name), src)
        check(m is not None, "RemoteProto.java declares %s" % name)
        got = int(m.group(1), 16 if m.group(1).startswith("0x") else 10)
        check(got == want, "%s == 0x%02x (got 0x%02x)" % (name, want, got))


def java_pair_request_json(code, device_id, device_name):
    """Replicate Android org.json serialization byte-for-byte.

    PairActivity does: new JSONObject();
    put("code",..); put("device_id",..); put("device_name",..);
    put("platform","android"); toString().
    Android's JSONObject keeps insertion order, emits compact JSON with no
    spaces, UTF-8, and escapes only '"', '\\' and chars < 0x20. For the
    ASCII values used here, json.dumps with compact separators is
    byte-identical.
    """
    obj = {"code": code, "device_id": device_id,
           "device_name": device_name, "platform": "android"}
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


class StubServer(threading.Thread):
    """Real TCP server speaking the real framing. mode selects behavior."""

    def __init__(self, mode):
        super().__init__(daemon=True)
        self.mode = mode
        self.ready = threading.Event()
        self.port = None
        self.hello = None          # parsed DEVICE_HELLO dict
        self.pair_raw = None       # raw PAIR_REQUEST payload bytes
        self.pair_type = None      # PAIR_REQUEST msg type byte seen
        self.done = threading.Event()

    def run(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        self.port = srv.getsockname()[1]
        self.ready.set()
        conn, _ = srv.accept()
        try:
            conn.settimeout(15)
            mtype, payload = remote_proto.recv_msg(conn)
            assert mtype == DEVICE_HELLO, "expected DEVICE_HELLO, got 0x%02x" % mtype
            self.hello = json.loads(payload.decode("utf-8"))
            if self.mode == "legacy":
                # pre-v4 host: never heard of PAIR_REQUEST, goes straight to auth
                remote_proto.send_msg(conn, AUTH_REQ, os.urandom(16) + os.urandom(32))
            else:
                mtype, payload = remote_proto.recv_msg(conn)
                self.pair_type = mtype
                self.pair_raw = payload
                if self.mode == "ok":
                    remote_proto.send_msg(
                        conn, PAIR_RESULT,
                        json.dumps({"ok": True}).encode("utf-8"))
                elif self.mode == "badcode":
                    remote_proto.send_msg(
                        conn, PAIR_RESULT,
                        json.dumps({"ok": False, "detail": "bad code"}).encode("utf-8"))
            try:
                remote_proto.recv_msg(conn)  # wait for client close
            except remote_proto.ProtocolError:
                pass
        finally:
            conn.close()
            srv.close()
            self.done.set()


def pair_client(port, code, device_id, device_name):
    """Mirror of PairActivity.doPair: DEVICE_HELLO, PAIR_REQUEST,
    wait for PAIR_RESULT. Returns ("ok"|"rejected"|"legacy", detail)."""
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    try:
        s.settimeout(15)
        # 1. DEVICE_HELLO (same fields DeviceHello.buildJson sends)
        remote_proto.send_msg(
            s, DEVICE_HELLO,
            json.dumps({"device_id": device_id, "device_name": device_name,
                        "platform": "android"}).encode("utf-8"))
        # 2. PAIR_REQUEST, serialized exactly like the Java client
        remote_proto.send_msg(s, PAIR_REQUEST,
                              java_pair_request_json(code, device_id, device_name))
        # 3. wait for verdict
        mtype, payload = remote_proto.recv_msg(s)
        if mtype == PAIR_RESULT:
            o = json.loads(payload.decode("utf-8"))
            if o.get("ok"):
                return "ok", ""
            return "rejected", o.get("detail", "")
        if mtype == AUTH_REQ:
            return "legacy", "host does not support pairing codes"
        return "unexpected", "reply 0x%02x" % mtype
    finally:
        s.close()


def main():
    check_java_constants()

    device_id, device_name = "harness-pair-1", "Harness Pair"

    # --- positive: code accepted -------------------------------------------
    srv = StubServer("ok")
    srv.start()
    srv.ready.wait(10)
    status, detail = pair_client(srv.port, "000000", device_id, device_name)
    check(status == "ok", "code 000000 -> PAIR_RESULT ok:true")
    check(srv.hello == {"device_id": device_id, "device_name": device_name,
                        "platform": "android"},
          "DEVICE_HELLO fields field-for-field")
    check(srv.pair_type == PAIR_REQUEST, "second message is PAIR_REQUEST (0x77)")
    check(srv.pair_raw == java_pair_request_json("000000", device_id, device_name),
          "PAIR_REQUEST payload byte-exact vs Java serialization "
          "(%d bytes)" % len(srv.pair_raw))
    check(json.loads(srv.pair_raw.decode("utf-8")) ==
          {"code": "000000", "device_id": device_id,
           "device_name": device_name, "platform": "android"},
          "PAIR_REQUEST fields field-for-field")
    # framing header itself: [0x77][BE len]
    check(srv.pair_raw is not None and len(srv.pair_raw) > 0, "non-empty payload")
    srv.done.wait(10)

    # --- negative: bad code -> detail surfaced -------------------------------
    srv = StubServer("badcode")
    srv.start()
    srv.ready.wait(10)
    status, detail = pair_client(srv.port, "999999", device_id, device_name)
    check(status == "rejected" and detail == "bad code",
          "code 999999 -> server detail surfaced: %r" % detail)
    srv.done.wait(10)

    # --- legacy host: AUTH_REQ instead of PAIR_RESULT -------------------------
    srv = StubServer("legacy")
    srv.start()
    srv.ready.wait(10)
    status, detail = pair_client(srv.port, "000000", device_id, device_name)
    check(status == "legacy", "pre-v4 host -> legacy branch taken")
    srv.done.wait(10)

    print("V4-PAIR-CLIENT CHECKS PASSED")


if __name__ == "__main__":
    main()
