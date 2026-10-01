# Remote Wire Protocol v1

Viewer-agnostic: the Linux tkinter viewer (`viewer/remote_viewer.py`) and the
Android viewer (`android/`) both speak this exact protocol. Any new viewer
must implement it byte-for-byte.

## Transport

Plain TCP, default port **47800**. Encryption is provided by the network
layer: peers connect over **Tailscale** (WireGuard), so nothing is exposed
publicly. The protocol adds its own password authentication on top.

## Framing

Every message: `[1-byte type][4-byte big-endian payload length][payload]`.

Max payload: 8 MiB. Either side must drop the connection on framing errors.

## Message types

| Type | Name         | Dir            | Payload |
|------|--------------|----------------|---------|
| 0x01 | AUTH_REQ     | server → client | `salt(16) \|\| nonce(32)` |
| 0x02 | AUTH_RESP    | client → server | `HMAC-SHA256(key, nonce)` (32 bytes) |
| 0x03 | AUTH_OK      | server → client | `b"REMOTE/1"` |
| 0x04 | AUTH_FAIL    | server → client | UTF-8 reason string |
| 0x10 | FRAME        | server → client | JPEG bytes (a complete screen frame) |
| 0x11 | INPUT        | client → server | UTF-8 JSON input event (see below) |
| 0x20 | PING         | client → server | opaque token bytes |
| 0x21 | PONG         | server → client | echo of the PING payload |
| 0xFF | DISCONNECT   | either          | empty |

## Authentication

1. Client connects. Server replies `AUTH_REQ` with a fresh 16-byte salt and
   32-byte nonce.
2. Client derives `key = PBKDF2-HMAC-SHA256(password, salt, 200000)` and
   replies `AUTH_RESP = HMAC-SHA256(key, nonce)`.
3. Server compares against `HMAC-SHA256(stored_key, nonce)` with a
   constant-time compare. Match → `AUTH_OK`; mismatch → `AUTH_FAIL` and the
   connection is closed.

The server stores only `salt` + derived `key` (`/etc/remote/host.conf`,
mode 0600) — never the password. Set it with `remote-set-password`.

## Frames

The server captures the screen (~12 fps), JPEG-encodes (quality ~60), and
sends a `FRAME` only when the frame changed since the last one. The viewer
keeps displaying the last frame until a new one arrives. Payload is a plain
JPEG (starts `FF D8`, ends `FF D9`) — the viewer learns the host resolution
from the image itself.

## Input events (client → server)

JSON object with a `t` field. Coordinates are in **host-frame pixels**.

```json
{"t": "move", "x": 123, "y": 456}
{"t": "click", "button": "left|middle|right", "down": true}
{"t": "scroll", "dx": 0, "dy": -1}
{"t": "key", "key": "a", "down": true}
```

Key names: a single printable character (`"a"`, `"A"`, `"5"`, `" "`) or one of
`Return BackSpace Tab Escape space Up Down Left Right Delete Home End
Page_Up Page_Down Caps_Lock Shift_L Shift_R Control_L Control_R Alt_L Alt_R
Super_L Super_R F1..F12`.

Viewer mappings (both viewers behave identically):
- tap / left mouse button → `move` + `click(left, down)` + `click(left, up)`
- long-press / right mouse button → `move` + `click(right, down/up)`
- drag / mouse motion → throttled `move` events
- typed text → per-character `key` down/up (`Shift_L` wrapped for capitals)
- special keys → named `key` down/up

Unknown event types are logged and ignored by the host; malformed JSON
closes nothing — the host just logs and continues.
