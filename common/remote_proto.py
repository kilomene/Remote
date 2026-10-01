"""Shared wire protocol for Remote v3.

Remote is an AnyDesk-style remote desktop for Linux. Transport runs over
Tailscale (WireGuard-encrypted P2P); this protocol adds password auth,
screen frames and input events on top of a plain TCP connection.

Wire framing: [1-byte msg type][4-byte big-endian payload length][payload]
"""

import hashlib
import hmac
import json
import os
import socket
import struct

PORT = 47800
PROTOCOL_VERSION = 3
PROTOCOL_ID = b"REMOTE/3"  # v1 hosts send b"REMOTE/1"; clients accept any REMOTE/N

# Message types: server -> client
AUTH_REQ = 0x01  # payload: salt(16) || nonce(32)
AUTH_OK = 0x03  # payload: PROTOCOL_ID
AUTH_FAIL = 0x04  # payload: UTF-8 reason
FRAME = 0x10  # payload: JPEG bytes
PONG = 0x21  # payload: echo of PING payload
# client -> server
AUTH_RESP = 0x02  # payload: HMAC-SHA256(key, nonce)
INPUT = 0x11  # payload: UTF-8 JSON input event
PING = 0x20  # payload: opaque, echoed back
# either direction
DISCONNECT = 0xFF  # payload: empty

# --- v2 additions (v1 types above are byte-identical) ---
# pairing (0x30-0x3F)
DEVICE_HELLO = 0x30  # c->s, first msg: JSON {device_id, device_name, platform}
# clipboard (0x40-0x4F), text only
CLIPBOARD_SET = 0x40  # either: JSON {text}
# file transfer (0x50-0x5F)
FILE_LIST = 0x50       # c->s: JSON {path}
FILE_GET = 0x51        # c->s: JSON {path, offset}
FILE_DATA = 0x52       # either: raw chunk bytes (<= 256 KiB)
FILE_DONE = 0x53       # either: JSON {path, size}
FILE_PUT = 0x54        # c->s: JSON {path, size}
FILE_MKDIR = 0x55      # c->s: JSON {path}
FILE_DELETE = 0x56     # c->s: JSON {path}
FILE_RENAME = 0x57     # c->s: JSON {from, to}
FILE_LIST_RESP = 0x58  # s->c: JSON {path, entries: [{name, size, dir, mtime}]}
FILE_META = 0x59       # s->c: JSON {path, size}
FILE_ERROR = 0x5A      # s->c: JSON {op, reason}

# --- v3 additions (v1/v2 types above are byte-identical) ---
# system commands (0x60)
SYSTEM_CMD = 0x60   # c->s: UTF-8 JSON {cmd, args?}
# remote terminal (0x61-0x63); 0x61 is shared: direction distinguishes
TERMINAL_OPEN = 0x61    # c->s: UTF-8 JSON {cols, rows}
TERMINAL_OPENED = 0x61  # s->c: UTF-8 JSON {session}
TERMINAL_DATA = 0x62    # either: UTF-8 JSON {session, data: base64(raw pty bytes)}
TERMINAL_CLOSE = 0x63   # either: UTF-8 JSON {session}
# session chat (0x64)
CHAT_MSG = 0x64  # either: UTF-8 JSON {from, text, ts}
# agent / automation status (0x65-0x66)
AGENT_QUERY = 0x65   # c->s: UTF-8 JSON {} or {services:[...]} to override list
AGENT_STATUS = 0x66  # s->c: UTF-8 JSON {cpu_pct, mem_total_mb, mem_used_mb,
                     #   disk_total_gb, disk_used_gb, net_rx_bps, net_tx_bps,
                     #   services:[{name, active}], ts}
# displays (0x67-0x68)
DISPLAYS_QUERY = 0x67  # c->s: empty or {}
DISPLAYS_LIST = 0x68   # s->c: UTF-8 JSON {displays:[{id, name, w, h, primary}], active}
# system command response (0x6F)
SYSTEM_RESP = 0x6F  # s->c: UTF-8 JSON {cmd, ok, detail?}

# --- multi-user / permissions (0x80-0x84, enforced by the host) ---
PERMS_SET = 0x80        # c->s: JSON {device_id, permissions:{view,mouse,keyboard,clipboard,files,terminal,system}}
PERMS_RESP = 0x81       # s->c: JSON {device_id, ok, detail?}
PERMS_DENIED = 0x82     # s->c: JSON {op, reason}
PERMS_LIST = 0x83       # c->s: JSON {}
PERMS_LIST_RESP = 0x84  # s->c: JSON {devices:[{device_id, device_name, platform, first_seen, last_seen, permissions}]}

# The seven permission flags. Every device carries all seven (booleans).
PERMISSION_FLAGS = ("view", "mouse", "keyboard", "clipboard", "files",
                    "terminal", "system")

# Reserved ranges (see PROTOCOL.md): 0x69-0x6E spare in the v3 block,
# 0x70-0x7F audio (planned), 0x85-0x8F reserved (multi-user extensions),
# 0x90-0x9F WebRTC/STUN/TURN, 0xC0-0xFE future.

HEADER = struct.Struct(">BI")
MAX_PAYLOAD = 8 * 1024 * 1024  # 8 MiB sanity cap

PBKDF2_ITERATIONS = 200_000
SALT_LEN = 16
NONCE_LEN = 32


class ProtocolError(Exception):
    pass


class AuthError(ProtocolError):
    pass


def derive_key(password: str, salt: bytes, iterations: int = PBKDF2_ITERATIONS) -> bytes:
    """Derive the auth key from a password. The server stores only this key."""
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


def send_msg(sock: socket.socket, mtype: int, payload: bytes = b"") -> None:
    if len(payload) > MAX_PAYLOAD:
        raise ProtocolError("payload too large")
    sock.sendall(HEADER.pack(mtype, len(payload)) + payload)


def _recvall(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ProtocolError("connection closed")
        buf += chunk
    return bytes(buf)


def recv_msg(sock: socket.socket):
    """Return (mtype, payload). Raises ProtocolError on EOF / bad framing."""
    header = _recvall(sock, HEADER.size)
    mtype, length = HEADER.unpack(header)
    if length > MAX_PAYLOAD:
        raise ProtocolError("payload length %d exceeds cap" % length)
    payload = _recvall(sock, length) if length else b""
    return mtype, payload


def server_handshake(conn: socket.socket, stored_key: bytes, salt: bytes,
                     timeout: float = 15.0) -> dict:
    """Server side of auth. Returns the client's device info dict.

    A v2 client sends DEVICE_HELLO first; v1 clients send nothing and are
    recorded as device_id "unknown". Raises AuthError on failure.
    """
    # Optional pre-auth device announcement (v2). v1 clients stay silent,
    # so wait only briefly before proceeding.
    device = {"device_id": "unknown", "device_name": "unknown", "platform": "unknown"}
    conn.settimeout(1.5)
    try:
        mtype, payload = recv_msg(conn)
        if mtype == DEVICE_HELLO:
            try:
                info = json.loads(payload.decode("utf-8"))
                for k in ("device_id", "device_name", "platform"):
                    if info.get(k):
                        device[k] = str(info[k])[:128]
            except (ValueError, AttributeError):
                pass
        else:
            raise AuthError("expected DEVICE_HELLO or AUTH silence, got 0x%02x" % mtype)
    except socket.timeout:
        pass  # v1 client: silent, proceed without device info
    except ProtocolError as exc:
        if "closed" in str(exc):
            raise AuthError("connection closed before auth: %s" % exc)
        # any other framing weirdness pre-auth: proceed without device info
    conn.settimeout(timeout)
    nonce = os.urandom(NONCE_LEN)
    send_msg(conn, AUTH_REQ, salt + nonce)
    try:
        mtype, payload = recv_msg(conn)
    except ProtocolError as exc:
        raise AuthError("no auth response: %s" % exc)
    if mtype != AUTH_RESP:
        raise AuthError("expected AUTH_RESP, got 0x%02x" % mtype)
    expected = hmac.new(stored_key, nonce, hashlib.sha256).digest()
    if len(payload) != len(expected) or not hmac.compare_digest(payload, expected):
        raise AuthError("bad password")
    send_msg(conn, AUTH_OK, PROTOCOL_ID)
    conn.settimeout(None)
    return device


def client_handshake(sock: socket.socket, password: str, timeout: float = 15.0,
                     device: dict = None) -> None:
    """Client side of auth. Raises AuthError on failure.

    device, if given, is sent as DEVICE_HELLO before auth:
    {"device_id":..., "device_name":..., "platform":...}.
    """
    sock.settimeout(timeout)
    if device:
        send_msg(sock, DEVICE_HELLO, json.dumps(device).encode("utf-8"))
    mtype, payload = recv_msg(sock)
    if mtype == AUTH_FAIL:
        raise AuthError("server refused: %s" % payload.decode("utf-8", "replace"))
    if mtype != AUTH_REQ or len(payload) != SALT_LEN + NONCE_LEN:
        raise AuthError("bad AUTH_REQ from server")
    salt, nonce = payload[:SALT_LEN], payload[SALT_LEN:]
    key = derive_key(password, salt)
    send_msg(sock, AUTH_RESP, hmac.new(key, nonce, hashlib.sha256).digest())
    mtype, payload = recv_msg(sock)
    sock.settimeout(None)
    if mtype == AUTH_OK:
        return
    if mtype == AUTH_FAIL:
        raise AuthError("auth failed: %s" % payload.decode("utf-8", "replace"))
    raise AuthError("unexpected reply 0x%02x during auth" % mtype)
