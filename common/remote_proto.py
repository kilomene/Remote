"""Shared wire protocol for Remote v1.

Remote is an AnyDesk-style remote desktop for Linux. Transport runs over
Tailscale (WireGuard-encrypted P2P); this protocol adds password auth,
screen frames and input events on top of a plain TCP connection.

Wire framing: [1-byte msg type][4-byte big-endian payload length][payload]
"""

import hashlib
import hmac
import os
import socket
import struct

PORT = 47800
PROTOCOL_VERSION = 1
PROTOCOL_ID = b"REMOTE/1"

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
                     timeout: float = 15.0) -> None:
    """Server side of auth. Raises AuthError on failure."""
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


def client_handshake(sock: socket.socket, password: str, timeout: float = 15.0) -> None:
    """Client side of auth. Raises AuthError on failure."""
    sock.settimeout(timeout)
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
