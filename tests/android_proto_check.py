#!/usr/bin/env python3
"""Independent protocol-conformance check for Remote v1.

Replays EXACTLY what the Android viewer (RemoteProto.java / StreamActivity.java)
sends, using a from-scratch implementation (no shared code with host/viewer).
Verifies: handshake, auth, JPEG frames, tap/right-click/key input shapes,
PING/PONG, DISCONNECT.
"""
import hashlib
import hmac
import json
import socket
import struct
import sys

AUTH_REQ, AUTH_RESP, AUTH_OK, AUTH_FAIL = 0x01, 0x02, 0x03, 0x04
FRAME, INPUT, PING, PONG, DISCONNECT = 0x10, 0x11, 0x20, 0x21, 0xFF


def send(sock, mtype, payload=b""):
    sock.sendall(struct.pack(">BI", mtype, len(payload)) + payload)


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


def check(cond, label):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        sys.exit(1)


def main():
    host, port, password, nframes = sys.argv[1], int(sys.argv[2]), sys.argv[3], int(sys.argv[4])
    s = socket.create_connection((host, port), timeout=15)

    # 1. handshake, exactly like RemoteProto.connect()
    mtype, payload = recv(s)
    check(mtype == AUTH_REQ and len(payload) == 48, "AUTH_REQ salt(16)||nonce(32)")
    salt, nonce = payload[:16], payload[16:]
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200000)
    send(s, AUTH_RESP, hmac.new(key, nonce, hashlib.sha256).digest())
    mtype, payload = recv(s)
    check(mtype == AUTH_OK, "AUTH_OK after correct password")

    # 2. frames are valid JPEGs
    got = 0
    s.settimeout(15)
    while got < nframes:
        mtype, payload = recv(s)
        if mtype == FRAME:
            check(payload[:2] == b"\xff\xd8" and payload[-2:] == b"\xff\xd9",
                  "frame %d valid JPEG (%d bytes)" % (got, len(payload)))
            got += 1
    s.settimeout(None)

    # 3. input events in the exact shapes StreamActivity sends
    def inp(ev):
        send(s, INPUT, json.dumps(ev).encode())

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

    # 4. ping/pong round-trip
    import os
    token = os.urandom(8)
    send(s, PING, token)
    s.settimeout(10)
    ok = False
    while not ok:
        mtype, payload = recv(s)
        if mtype == PONG and payload == token:
            ok = True
        elif mtype != FRAME:
            check(False, "unexpected msg during ping 0x%02x" % mtype)
    check(ok, "PING/PONG round-trip")
    s.settimeout(None)

    # 5. clean disconnect
    send(s, DISCONNECT)
    s.close()
    print("ALL PROTOCOL CHECKS PASSED")


if __name__ == "__main__":
    main()
