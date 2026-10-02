#!/usr/bin/env python3
"""Independent protocol-conformance check for Remote v2.

Replays EXACTLY what the Android viewer sends, using a from-scratch
implementation (no shared code with host/viewer).

v1: handshake (silent client), auth, JPEG frames, tap/right-click/key input
    shapes, PING/PONG, DISCONNECT.
v2: DEVICE_HELLO + auth, clipboard set/get round-trip across two clients,
    file list/get/put/resume/mkdir/rename/delete, path-traversal rejection,
    wrong-password rejection.
"""
import hashlib
import hmac
import json
import os
import socket
import struct
import sys

AUTH_REQ, AUTH_RESP, AUTH_OK, AUTH_FAIL = 0x01, 0x02, 0x03, 0x04
FRAME, INPUT, PING, PONG, DISCONNECT = 0x10, 0x11, 0x20, 0x21, 0xFF
DEVICE_HELLO = 0x30
CLIPBOARD_SET = 0x40
SESSION_TOKEN, SESSION_ROTATE = 0x85, 0x86  # v4: s->c background traffic
FILE_LIST, FILE_GET, FILE_DATA = 0x50, 0x51, 0x52
FILE_DONE, FILE_PUT = 0x53, 0x54
FILE_MKDIR, FILE_DELETE, FILE_RENAME = 0x55, 0x56, 0x57
FILE_LIST_RESP, FILE_META, FILE_ERROR = 0x58, 0x59, 0x5A

# background traffic the harness must tolerate while waiting
BACKGROUND_SKIP = (FRAME, PONG, SESSION_TOKEN, SESSION_ROTATE)


def send(sock, mtype, payload=b""):
    sock.sendall(struct.pack(">BI", mtype, len(payload)) + payload)


def send_json(sock, mtype, obj):
    send(sock, mtype, json.dumps(obj).encode())


def recvn(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise RuntimeError("eof")
        buf += chunk
    return buf


def recv(sock):
    mtype, length = struct.unpack(">BI", recvn(sock, 5))
    return mtype, recvn(sock, length) if length else b""


def recv_skip(sock, want, timeout=15):
    """Read messages until one of `want` arrives; skip background traffic
    (FRAME/PONG plus the v4 SESSION_TOKEN/SESSION_ROTATE)."""
    sock.settimeout(timeout)
    while True:
        mtype, payload = recv(sock)
        if mtype in want:
            sock.settimeout(None)
            return mtype, payload
        if mtype not in BACKGROUND_SKIP:
            raise RuntimeError("unexpected msg 0x%02x while waiting %s" %
                               (mtype, ["0x%02x" % w for w in want]))


def check(cond, label):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        sys.exit(1)


def do_handshake(sock, password, device=None):
    """Full client auth. Returns the AUTH_OK payload."""
    if device is not None:
        send_json(sock, DEVICE_HELLO, device)
    mtype, payload = recv(sock)
    check(mtype == AUTH_REQ and len(payload) == 48, "AUTH_REQ salt(16)||nonce(32)")
    salt, nonce = payload[:16], payload[16:]
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200000)
    send(sock, AUTH_RESP, hmac.new(key, nonce, hashlib.sha256).digest())
    mtype, payload = recv(sock)
    check(mtype == AUTH_OK, "AUTH_OK after correct password")
    return payload


def v1_checks(host, port, password, nframes):
    s = socket.create_connection((host, port), timeout=15)
    do_handshake(s, password)  # silent v1 client: no DEVICE_HELLO

    got = 0
    s.settimeout(15)
    while got < nframes:
        mtype, payload = recv(s)
        if mtype == FRAME:
            check(payload[:2] == b"\xff\xd8" and payload[-2:] == b"\xff\xd9",
                  "frame %d valid JPEG (%d bytes)" % (got, len(payload)))
            got += 1
    s.settimeout(None)

    def inp(ev):
        send_json(s, INPUT, ev)

    inp({"t": "move", "x": 100, "y": 200})                                  # onMove
    inp({"t": "move", "x": 100, "y": 200})                                  # onTap: move
    inp({"t": "click", "button": "left", "down": True})                      # onTap: down
    inp({"t": "click", "button": "left", "down": False})                     # onTap: up
    inp({"t": "move", "x": 50, "y": 60})                                    # onRightClick: move
    inp({"t": "click", "button": "right", "down": True})                     # onRightClick
    inp({"t": "click", "button": "right", "down": False})
    inp({"t": "key", "key": "a", "down": True})                              # onText('a')
    inp({"t": "key", "key": "a", "down": False})
    inp({"t": "key", "key": "Shift_L", "down": True})                        # onText('A')
    inp({"t": "key", "key": "a", "down": True})
    inp({"t": "key", "key": "a", "down": False})
    inp({"t": "key", "key": "Shift_L", "down": False})
    inp({"t": "key", "key": "Return", "down": True})                        # hardware Enter
    inp({"t": "key", "key": "Return", "down": False})
    inp({"t": "key", "key": "BackSpace", "down": True})                      # soft-kb delete
    inp({"t": "key", "key": "BackSpace", "down": False})
    print("PASS sent 17 Android-shaped input events")

    token = os.urandom(8)
    send(s, PING, token)
    mtype, payload = recv_skip(s, (PONG,), timeout=10)
    check(payload == token, "PING/PONG round-trip")

    send(s, DISCONNECT)
    s.close()
    print("PASS v1 checks complete")


def v2_checks(host, port, password):
    device = {"device_id": "harness-android-1", "device_name": "Harness Phone",
              "platform": "android"}

    # --- pairing: DEVICE_HELLO then auth -------------------------------------
    a = socket.create_connection((host, port), timeout=15)
    ok_payload = do_handshake(a, password, device)
    check(ok_payload.startswith(b"REMOTE/") and ok_payload >= b"REMOTE/2",
          "AUTH_OK advertises REMOTE/2+, got %r" % ok_payload)

    # --- clipboard: A sets, B (new client) receives on join -------------------
    send_json(a, CLIPBOARD_SET, {"text": "clip-hello-123"})
    b = socket.create_connection((host, port), timeout=15)
    do_handshake(b, password, {"device_id": "harness-android-2",
                               "device_name": "Harness Two", "platform": "android"})
    mtype, payload = recv_skip(b, (CLIPBOARD_SET,), timeout=15)
    check(json.loads(payload.decode())["text"] == "clip-hello-123",
          "clipboard round-trip across clients")

    # --- file transfer ---------------------------------------------------------
    root = "harness"
    data = os.urandom(600 * 1024)  # 600KB: forces multiple 256KB chunks

    send_json(a, FILE_LIST, {"path": root})
    mtype, payload = recv_skip(a, (FILE_LIST_RESP, FILE_ERROR))
    # may not exist yet: either is fine, we mkdir next
    if mtype == FILE_ERROR:
        print("PASS FILE_LIST on missing dir -> FILE_ERROR (acceptable)")
    else:
        check(isinstance(json.loads(payload.decode())["entries"], list),
              "FILE_LIST_RESP entries list")

    send_json(a, FILE_MKDIR, {"path": root + "/sub"})
    mtype, payload = recv_skip(a, (FILE_DONE, FILE_ERROR))
    check(mtype == FILE_DONE, "FILE_MKDIR acknowledged")

    # upload
    rpath = root + "/sub/up.bin"
    send_json(a, FILE_PUT, {"path": rpath, "size": len(data)})
    for i in range(0, len(data), 200 * 1024):
        send(a, FILE_DATA, data[i:i + 200 * 1024])
    send_json(a, FILE_DONE, {"path": rpath, "size": len(data)})
    mtype, payload = recv_skip(a, (FILE_DONE, FILE_ERROR))
    check(mtype == FILE_DONE and json.loads(payload.decode())["size"] == len(data),
          "FILE_PUT upload acknowledged (%d bytes)" % len(data))

    # list shows the file
    send_json(a, FILE_LIST, {"path": root + "/sub"})
    mtype, payload = recv_skip(a, (FILE_LIST_RESP,))
    entries = json.loads(payload.decode())["entries"]
    check(any(e["name"] == "up.bin" and e["size"] == len(data) and not e["dir"]
              for e in entries),
          "FILE_LIST shows uploaded file with correct size")

    # download full
    send_json(a, FILE_GET, {"path": rpath, "offset": 0})
    mtype, payload = recv_skip(a, (FILE_META, FILE_ERROR))
    check(mtype == FILE_META and json.loads(payload.decode())["size"] == len(data),
          "FILE_META size matches")
    got = b""
    while True:
        mtype, payload = recv_skip(a, (FILE_DATA, FILE_DONE, FILE_ERROR))
        if mtype == FILE_DATA:
            got += payload
        elif mtype == FILE_DONE:
            break
        else:
            check(False, "download failed: %s" % payload.decode())
    check(got == data, "FILE_GET download bytes identical (%d)" % len(got))

    # resume from offset
    off = 100 * 1024
    send_json(a, FILE_GET, {"path": rpath, "offset": off})
    mtype, payload = recv_skip(a, (FILE_META,))
    got = b""
    while True:
        mtype, payload = recv_skip(a, (FILE_DATA, FILE_DONE, FILE_ERROR))
        if mtype == FILE_DATA:
            got += payload
        elif mtype == FILE_DONE:
            break
        else:
            check(False, "resume failed: %s" % payload.decode())
    check(got == data[off:], "FILE_GET resume from offset %d correct" % off)

    # rename + delete
    send_json(a, FILE_RENAME, {"from": rpath, "to": root + "/sub/moved.bin"})
    mtype, _ = recv_skip(a, (FILE_DONE, FILE_ERROR))
    check(mtype == FILE_DONE, "FILE_RENAME acknowledged")
    send_json(a, FILE_DELETE, {"path": root + "/sub/moved.bin"})
    mtype, _ = recv_skip(a, (FILE_DONE, FILE_ERROR))
    check(mtype == FILE_DONE, "FILE_DELETE acknowledged")

    # traversal rejected
    send_json(a, FILE_LIST, {"path": "../../etc"})
    mtype, payload = recv_skip(a, (FILE_ERROR,))
    check("escapes" in json.loads(payload.decode())["reason"],
          "path traversal rejected with FILE_ERROR")
    send_json(a, FILE_GET, {"path": "/etc/passwd"})
    mtype, payload = recv_skip(a, (FILE_ERROR,))
    check(mtype == FILE_ERROR, "absolute-path escape rejected")

    send(a, DISCONNECT)
    a.close()

    # wrong password, even with a fresh device_id, is rejected
    c = socket.create_connection((host, port), timeout=15)
    send_json(c, DEVICE_HELLO, {"device_id": "harness-evil",
                                "device_name": "Evil", "platform": "android"})
    mtype, payload = recv(c)
    check(mtype == AUTH_REQ, "AUTH_REQ still sent before rejecting")
    salt, nonce = payload[:16], payload[16:]
    key = hashlib.pbkdf2_hmac("sha256", b"wrong-password", salt, 200000)
    send(c, AUTH_RESP, hmac.new(key, nonce, hashlib.sha256).digest())
    mtype, payload = recv(c)
    check(mtype == AUTH_FAIL, "wrong password rejected (AUTH_FAIL)")
    c.close()

    send(b, DISCONNECT)
    b.close()
    print("PASS v2 checks complete")


def main():
    host, port, password, nframes = sys.argv[1], int(sys.argv[2]), sys.argv[3], int(sys.argv[4])
    v1_checks(host, port, password, nframes)
    v2_checks(host, port, password)
    print("ALL PROTOCOL CHECKS PASSED")


if __name__ == "__main__":
    main()
