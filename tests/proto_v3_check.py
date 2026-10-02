#!/usr/bin/env python3
"""Independent protocol-conformance check for Remote v3.

Replays EXACTLY what the Android viewer sends, using a from-scratch
implementation (no shared code with host/viewer).

v3: handshake advertises REMOTE/4 (the v4 host is a superset of v3),
    SYSTEM_CMD allowlist (every allowed command returns ok -- dry-run in
    self-test; "rm -rf /" is rejected;
    "service" only touches monitored_services), TERMINAL round-trip
    (open -> echo hello-v3 -> close) + 8-session cap, AGENT_QUERY ->
    AGENT_STATUS shape (+ services override), DISPLAYS_QUERY ->
    DISPLAYS_LIST (>= 1 display), CHAT_MSG relay across two clients
    (sender gets no echo) + chat log, PERMS_SET/PERMS_LIST round-trip and
    live enforcement (denied INPUT/SYSTEM_CMD/TERMINAL/FILE/CLIPBOARD get
    PERMS_DENIED; self-change rejected), wrong-password rejection.
"""
import base64
import hashlib
import hmac
import json
import os
import socket
import struct
import sys
import time

AUTH_REQ, AUTH_RESP, AUTH_OK, AUTH_FAIL = 0x01, 0x02, 0x03, 0x04
FRAME, INPUT, PING, PONG, DISCONNECT = 0x10, 0x11, 0x20, 0x21, 0xFF
DEVICE_HELLO = 0x30
CLIPBOARD_SET = 0x40
SYSTEM_CMD, SYSTEM_RESP = 0x60, 0x6F
TERMINAL_OPEN = 0x61   # shared id: direction distinguishes OPEN vs OPENED
TERMINAL_DATA, TERMINAL_CLOSE = 0x62, 0x63
CHAT_MSG = 0x64
AGENT_QUERY, AGENT_STATUS = 0x65, 0x66
DISPLAYS_QUERY, DISPLAYS_LIST = 0x67, 0x68
PERMS_SET, PERMS_RESP, PERMS_DENIED = 0x80, 0x81, 0x82
PERMS_LIST, PERMS_LIST_RESP = 0x83, 0x84
FILE_LIST = 0x50

# background traffic the harness must tolerate while waiting
SESSION_TOKEN, SESSION_ROTATE = 0x85, 0x86
BACKGROUND = (FRAME, PONG, CLIPBOARD_SET, TERMINAL_DATA, TERMINAL_CLOSE,
              CHAT_MSG, SESSION_TOKEN, SESSION_ROTATE)


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


def recv_until(sock, want, timeout=15, stash=None):
    """Read until a message of type in `want` arrives. Background traffic
    (FRAME/PONG/...) is skipped; TERMINAL_DATA is stashed per session when
    a stash dict is given. Anything else unexpected raises."""
    sock.settimeout(timeout)
    try:
        while True:
            mtype, payload = recv(sock)
            if mtype in want:
                return mtype, payload
            if mtype == TERMINAL_DATA and stash is not None:
                try:
                    d = json.loads(payload.decode())
                    stash.setdefault(d["session"], b"")
                    stash[d["session"]] += base64.b64decode(d["data"])
                except (ValueError, KeyError):
                    pass
                continue
            if mtype in BACKGROUND:
                continue
            raise RuntimeError("unexpected msg 0x%02x while waiting %s" %
                               (mtype, ["0x%02x" % w for w in want]))
    finally:
        sock.settimeout(None)


def check(cond, label):
    print(("PASS " if cond else "FAIL ") + label, flush=True)
    if not cond:
        sys.exit(1)


def do_handshake(sock, password, device):
    send_json(sock, DEVICE_HELLO, device)
    mtype, payload = recv(sock)
    check(mtype == AUTH_REQ and len(payload) == 48, "AUTH_REQ salt(16)||nonce(32)")
    salt, nonce = payload[:16], payload[16:]
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200000)
    send(sock, AUTH_RESP, hmac.new(key, nonce, hashlib.sha256).digest())
    mtype, payload = recv(sock)
    check(mtype == AUTH_OK, "AUTH_OK after correct password")
    return payload


def main():
    host, port, password = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    chat_log = os.environ.get("REMOTE_CHAT_LOG") or (
        sys.argv[4] if len(sys.argv) > 4 else "/tmp/remote-selftest-chat.log")

    a = socket.create_connection((host, port), timeout=15)
    ok_payload = do_handshake(a, password, {"device_id": "harness-v3-1",
                                            "device_name": "Harness V3",
                                            "platform": "android"})
    check(ok_payload == b"REMOTE/4", "AUTH_OK advertises REMOTE/4, got %r" % ok_payload)

    # --- displays -------------------------------------------------------------
    send(a, DISPLAYS_QUERY, b"{}")
    mtype, payload = recv_until(a, (DISPLAYS_LIST,), timeout=15)
    info = json.loads(payload.decode())
    check(isinstance(info.get("displays"), list) and len(info["displays"]) >= 1,
          "DISPLAYS_LIST has >= 1 display")
    d0 = info["displays"][0]
    check(all(k in d0 for k in ("id", "name", "w", "h", "primary")),
          "display entry has id/name/w/h/primary")
    check(info.get("active") in [d["id"] for d in info["displays"]],
          "DISPLAYS_LIST active id is valid")
    disp_id = d0["id"]
    print("  displays: %s" % [(d["id"], d["w"], d["h"]) for d in info["displays"]])

    # --- system commands: allowlist -------------------------------------------
    def sys_cmd(cmd, args=None):
        send_json(a, SYSTEM_CMD, {"cmd": cmd, "args": args or {}})
        mtype, payload = recv_until(a, (SYSTEM_RESP,), timeout=15)
        check(mtype == SYSTEM_RESP, "SYSTEM_RESP for %s" % cmd)
        return json.loads(payload.decode())

    allowed = [
        ("lock", {}),
        ("logout", {}),
        ("reboot", {}),
        ("shutdown", {}),
        ("suspend", {}),
        ("open-terminal", {}),
        ("open-browser", {"url": "https://example.com"}),
        ("open-app", {"name": "terminal"}),
        ("blank-screen", {"on": True}),
        ("blank-screen", {"on": False}),
        ("switch-display", {"id": disp_id}),
        ("service", {"name": "tailscaled", "action": "restart"}),
    ]
    for cmd, args in allowed:
        r = sys_cmd(cmd, args)
        check(r["ok"] is True and r.get("detail") == "dry-run" and r["cmd"] == cmd,
              "SYSTEM_CMD %s ok (dry-run)" % cmd)

    r = sys_cmd("rm -rf /")
    check(r["ok"] is False and "not allowed" in r.get("detail", ""),
          "SYSTEM_CMD 'rm -rf /' rejected")
    r = sys_cmd("open-browser", {"url": "ftp://evil.example/x"})
    check(r["ok"] is False, "SYSTEM_CMD open-browser with non-http url rejected")
    r = sys_cmd("open-app", {"name": "calculator"})
    check(r["ok"] is False, "SYSTEM_CMD open-app with unknown app rejected")
    r = sys_cmd("blank-screen", {})
    check(r["ok"] is False, "SYSTEM_CMD blank-screen without on:bool rejected")
    r = sys_cmd("switch-display", {"id": "no-such-display"})
    check(r["ok"] is False, "SYSTEM_CMD switch-display with bad id rejected")
    r = sys_cmd("service", {"name": "sshd", "action": "restart"})
    check(r["ok"] is False and "not allowed" in r.get("detail", ""),
          "SYSTEM_CMD service with non-monitored name rejected")
    r = sys_cmd("service", {"name": "tailscaled", "action": "kill"})
    check(r["ok"] is False and "not allowed" in r.get("detail", ""),
          "SYSTEM_CMD service with disallowed action rejected")
    r = sys_cmd("service", {"name": "tailscaled"})
    check(r["ok"] is False, "SYSTEM_CMD service without action rejected")
    print("PASS SYSTEM_CMD allowlist enforced")

    # --- terminal round-trip ---------------------------------------------------
    stash = {}

    def term_open():
        send_json(a, TERMINAL_OPEN, {"cols": 80, "rows": 24})
        mtype, payload = recv_until(a, (TERMINAL_OPEN,), timeout=15, stash=stash)
        # 0x61 is shared: an OPENED reply decodes to {"session": N}
        resp = json.loads(payload.decode())
        return resp

    resp = term_open()
    check(resp.get("session", 0) > 0, "TERMINAL_OPENED with session id")
    sess = resp["session"]

    send_json(a, TERMINAL_DATA, {"session": sess,
                                "data": base64.b64encode(b"echo hello-v3\n").decode()})
    deadline = time.time() + 20
    found = False
    a.settimeout(5)
    try:
        while time.time() < deadline:
            try:
                mtype, payload = recv(a)
            except socket.timeout:
                continue
            if mtype == TERMINAL_DATA:
                try:
                    d = json.loads(payload.decode())
                    if d.get("session") == sess:
                        stash.setdefault(sess, b"")
                        stash[sess] += base64.b64decode(d["data"])
                except (ValueError, KeyError):
                    pass
            elif mtype in BACKGROUND:
                continue
            else:
                check(False, "unexpected 0x%02x during terminal echo" % mtype)
            if b"hello-v3" in stash.get(sess, b""):
                found = True
                break
    finally:
        a.settimeout(None)
    check(found, "terminal echo round-trip (got 'hello-v3' back)")
    print("  terminal output sample: %r" % stash.get(sess, b"")[:60])

    send_json(a, TERMINAL_CLOSE, {"session": sess})
    time.sleep(0.5)

    # --- terminal session cap: 8 ----------------------------------------------
    sids = []
    for i in range(8):
        r = term_open()
        check(r.get("session", 0) > 0, "terminal session %d/8 opened" % (i + 1))
        sids.append(r["session"])
    r = term_open()
    check(r.get("session", 0) == 0 and "error" in r,
          "9th terminal session rejected (cap 8)")
    for sid in sids:
        send_json(a, TERMINAL_CLOSE, {"session": sid})
    time.sleep(1)
    print("PASS terminal session cap enforced")

    # --- agent status ----------------------------------------------------------
    send_json(a, AGENT_QUERY, {})
    mtype, payload = recv_until(a, (AGENT_STATUS,), timeout=20)
    st = json.loads(payload.decode())
    for k in ("cpu_pct", "mem_total_mb", "mem_used_mb", "disk_total_gb",
              "disk_used_gb", "net_rx_bps", "net_tx_bps", "services", "ts"):
        check(k in st, "AGENT_STATUS has key %s" % k)
    check(isinstance(st["services"], list) and
          all("name" in s and "active" in s for s in st["services"]),
          "AGENT_STATUS services is [{name, active}]")
    check(st["mem_total_mb"] > 0 and st["disk_total_gb"] > 0,
          "AGENT_STATUS mem/disk sane (mem=%sMB disk=%sGB)"
          % (st["mem_total_mb"], st["disk_total_gb"]))
    print("  status: cpu=%s%% mem=%s/%sMB services=%s"
          % (st["cpu_pct"], st["mem_used_mb"], st["mem_total_mb"],
             [(s["name"], s["active"]) for s in st["services"]]))

    send_json(a, AGENT_QUERY, {"services": ["tailscaled", "no-such-svc-xyz"]})
    mtype, payload = recv_until(a, (AGENT_STATUS,), timeout=20)
    st2 = json.loads(payload.decode())
    check([s["name"] for s in st2["services"]] == ["tailscaled", "no-such-svc-xyz"],
          "AGENT_QUERY services override honored")

    # --- chat relay: A sends, B receives, A gets no echo -----------------------
    b = socket.create_connection((host, port), timeout=15)
    do_handshake(b, password, {"device_id": "harness-v3-2",
                               "device_name": "Harness V3 Two",
                               "platform": "android"})
    # Barrier: B's PING -> PONG round-trip proves B's session is fully
    # registered server-side, so A's broadcast below cannot miss B in the
    # auth/register window (regression: broadcast raced _register).
    send(b, PING, b"barrier")
    mtype, payload = recv_until(b, (PONG,), timeout=10)
    check(payload == b"barrier", "PING/PONG barrier with B")
    chat = {"from": "harness-v3-1", "text": "chat-hello-v3", "ts": time.time()}
    send_json(a, CHAT_MSG, chat)
    mtype, payload = recv_until(b, (CHAT_MSG,), timeout=10)
    got = json.loads(payload.decode())
    check(got["text"] == "chat-hello-v3" and got["from"] == "harness-v3-1",
          "CHAT_MSG relayed A -> B")

    # A must not receive its own message back
    deadline = time.time() + 2
    a.settimeout(2)
    echo = False
    try:
        while time.time() < deadline:
            try:
                mtype, payload = recv(a)
            except socket.timeout:
                break
            if mtype == CHAT_MSG:
                echo = True
                break
            if mtype not in BACKGROUND:
                check(False, "unexpected 0x%02x after chat send" % mtype)
    finally:
        a.settimeout(None)
    check(not echo, "CHAT_MSG not echoed back to sender")

    # chat log got a JSON line
    time.sleep(0.5)
    logged = False
    try:
        with open(chat_log) as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("text") == "chat-hello-v3":
                    logged = True
                    break
    except OSError:
        pass
    check(logged, "chat message appended to log (%s)" % chat_log)

    # --- permissions: real live enforcement ------------------------------------
    def perms_set(sock, device_id, permissions):
        send_json(sock, PERMS_SET, {"device_id": device_id,
                                   "permissions": permissions})
        mtype, payload = recv_until(sock, (PERMS_RESP,), timeout=10)
        check(mtype == PERMS_RESP, "PERMS_RESP received")
        return json.loads(payload.decode())

    flags = ("view", "mouse", "keyboard", "clipboard", "files",
             "terminal", "system", "audio", "webcam", "apps",
             "automation", "camera")  # the twelve v4 flags (superset of v3's seven)
    full = {f: True for f in flags}
    restricted = dict(full)
    restricted.update({"mouse": False, "keyboard": False, "clipboard": False,
                       "files": False, "terminal": False, "system": False})

    # A restricts B to view-only
    r = perms_set(a, "harness-v3-2", restricted)
    check(r["ok"] is True and r["device_id"] == "harness-v3-2",
          "PERMS_SET view-only for B ok")

    def expect_denied(sock, mtype, obj, op, why):
        send_json(sock, mtype, obj)
        mtype2, payload2 = recv_until(sock, (PERMS_DENIED,), timeout=10)
        d = json.loads(payload2.decode())
        check(d["op"] == op and "denied" in d["reason"],
              "%s denied for restricted B (%s)" % (op, why))

    expect_denied(b, INPUT, {"t": "key", "key": "a", "down": True},
                  "INPUT", "keyboard=false")
    expect_denied(b, INPUT, {"t": "move", "x": 1, "y": 2}, "INPUT", "mouse=false")
    expect_denied(b, SYSTEM_CMD, {"cmd": "lock"}, "SYSTEM_CMD", "system=false")
    expect_denied(b, TERMINAL_OPEN, {"cols": 80, "rows": 24},
                  "TERMINAL_*", "terminal=false")
    expect_denied(b, FILE_LIST, {"path": "."}, "FILE_*", "files=false")
    expect_denied(b, CLIPBOARD_SET, {"text": "nope"}, "CLIPBOARD_SET",
                  "clipboard=false")

    # always-allowed traffic still works for restricted B
    token = os.urandom(8)
    send(b, PING, token)
    mtype, payload = recv_until(b, (PONG,), timeout=10)
    check(payload == token, "PING/PONG still allowed for restricted B")

    # a device cannot change its own permissions
    r = perms_set(b, "harness-v3-2", full)
    check(r["ok"] is False and r["detail"] == "cannot change own permissions",
          "PERMS_SET on self rejected")
    r = perms_set(a, "harness-v3-1", full)
    check(r["ok"] is False and "cannot change own permissions" in r["detail"],
          "PERMS_SET on own device rejected (A)")

    # unknown target / invalid flag shapes
    r = perms_set(a, "no-such-device", full)
    check(r["ok"] is False and r["detail"] == "unknown device",
          "PERMS_SET unknown device rejected")
    bad = dict(full)
    del bad["system"]
    r = perms_set(a, "harness-v3-2", bad)
    check(r["ok"] is False, "PERMS_SET with missing flag rejected")
    bad = dict(full)
    bad["mouse"] = "yes"
    r = perms_set(a, "harness-v3-2", bad)
    check(r["ok"] is False, "PERMS_SET with non-boolean flag rejected")

    # PERMS_LIST round-trip shows the live flags
    send(b, PERMS_LIST, b"{}")
    mtype, payload = recv_until(b, (PERMS_LIST_RESP,), timeout=10)
    devs = {d["device_id"]: d for d in json.loads(payload.decode())["devices"]}
    check("harness-v3-1" in devs and "harness-v3-2" in devs,
          "PERMS_LIST has both devices")
    check(devs["harness-v3-2"]["permissions"] == restricted,
          "PERMS_LIST shows B's restricted flags")
    check(devs["harness-v3-1"]["permissions"] == full,
          "PERMS_LIST shows A's full flags")
    check(all(k in devs["harness-v3-2"]
              for k in ("device_name", "platform", "first_seen", "last_seen")),
          "PERMS_LIST entries carry device metadata")

    # restore B to full (tidy; enforcement verified above)
    r = perms_set(a, "harness-v3-2", full)
    check(r["ok"] is True, "PERMS_SET restore ok")
    print("PASS permission enforcement end-to-end")

    # --- wrong password still rejected -----------------------------------------
    c = socket.create_connection((host, port), timeout=15)
    send_json(c, DEVICE_HELLO, {"device_id": "harness-v3-evil",
                                "device_name": "Evil", "platform": "android"})
    mtype, payload = recv(c)
    check(mtype == AUTH_REQ, "AUTH_REQ still sent before rejecting")
    salt, nonce = payload[:16], payload[16:]
    key = hashlib.pbkdf2_hmac("sha256", b"wrong-password", salt, 200000)
    send(c, AUTH_RESP, hmac.new(key, nonce, hashlib.sha256).digest())
    mtype, payload = recv(c)
    check(mtype == AUTH_FAIL, "wrong password rejected (AUTH_FAIL)")
    c.close()

    send(a, DISCONNECT)
    a.close()
    send(b, DISCONNECT)
    b.close()
    print("V3 CHECKS PASSED")


if __name__ == "__main__":
    main()
