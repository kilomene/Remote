# Remote Wire Protocol

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

## Versions

- **v1** (`AUTH_OK` payload `b"REMOTE/1"`): screen, input, ping.
- **v2** (`AUTH_OK` payload `b"REMOTE/2"`): v1 plus device pairing,
  clipboard sync, and file transfer.

All v1 message types are byte-identical in v2. Clients must accept any
`AUTH_OK` payload starting with `b"REMOTE/"` and enable features based on
the version number. A v1 client talking to a v2 host simply never sends
v2 messages; a v2 client talking to a v1 host must degrade gracefully
(no response to v2 messages means "not supported").

## Message types

### v1 (unchanged)

| Type | Name         | Dir            | Payload |
|------|--------------|----------------|---------|
| 0x01 | AUTH_REQ     | server → client | `salt(16) \|\| nonce(32)` |
| 0x02 | AUTH_RESP    | client → server | `HMAC-SHA256(key, nonce)` (32 bytes) |
| 0x03 | AUTH_OK      | server → client | `b"REMOTE/1"` or `b"REMOTE/2"` |
| 0x04 | AUTH_FAIL    | server → client | UTF-8 reason string |
| 0x10 | FRAME        | server → client | JPEG bytes (a complete screen frame) |
| 0x11 | INPUT        | client → server | UTF-8 JSON input event (see below) |
| 0x20 | PING         | client → server | opaque token bytes |
| 0x21 | PONG         | server → client | echo of the PING payload |
| 0xFF | DISCONNECT   | either          | empty |

### v2 — pairing (0x30–0x3F)

| Type | Name         | Dir            | Payload |
|------|--------------|----------------|---------|
| 0x30 | DEVICE_HELLO | client → server | UTF-8 JSON `{device_id, device_name, platform}` |

`DEVICE_HELLO` is the first message a v2 client sends, immediately after
TCP connect and before auth. The host records the device; on successful
password auth the device is added to the trusted-device store
(trust-on-first-use via password). Clients that skip it are recorded as
`device_id: "unknown"`. Explicit accept/reject approval is deferred
(the host is headless) — see FEATURES.md.

### v2 — clipboard (0x40–0x4F), text only for now

| Type | Name         | Dir            | Payload |
|------|--------------|----------------|---------|
| 0x40 | CLIPBOARD_SET | either         | UTF-8 JSON `{text}` |

Both sides push `{text}` when the local clipboard changes and apply
received text to the local clipboard. Each side must suppress echo loops
(ignore a change that matches the last text it sent or received).

### v2 — file transfer (0x50–0x5F)

| Type | Name           | Dir            | Payload |
|------|----------------|----------------|---------|
| 0x50 | FILE_LIST      | client → server | UTF-8 JSON `{path}` |
| 0x58 | FILE_LIST_RESP | server → client | UTF-8 JSON `{path, entries: [{name, size, dir, mtime}]}` |
| 0x51 | FILE_GET       | client → server | UTF-8 JSON `{path, offset}` |
| 0x59 | FILE_META      | server → client | UTF-8 JSON `{path, size}` |
| 0x52 | FILE_DATA      | either          | raw chunk bytes (≤ 256 KiB) |
| 0x53 | FILE_DONE      | either          | UTF-8 JSON `{path, size}` |
| 0x54 | FILE_PUT       | client → server | UTF-8 JSON `{path, size}` |
| 0x55 | FILE_MKDIR     | client → server | UTF-8 JSON `{path}` |
| 0x56 | FILE_DELETE    | client → server | UTF-8 JSON `{path}` |
| 0x57 | FILE_RENAME    | client → server | UTF-8 JSON `{from, to}` |
| 0x5A | FILE_ERROR     | server → client | UTF-8 JSON `{op, reason}` |

Flows:

- **Download**: client sends `FILE_GET {path, offset}` (offset 0 for a fresh
  download, >0 to resume). Server replies `FILE_META {path, size}`, then
  `FILE_DATA` chunks, then `FILE_DONE`. Progress = bytes_received / size.
- **Upload**: client sends `FILE_PUT {path, size}`, streams `FILE_DATA`
  chunks, then `FILE_DONE`. Server replies `FILE_DONE` (or `FILE_ERROR`).
- `FILE_MKDIR` / `FILE_DELETE` / `FILE_RENAME` are acknowledged with
  `FILE_DONE` (for rename, `{from, to}` echoed back in place of `{path,size}`).

Path sandboxing: the host jails all file operations under a root directory
(the logged-in user's home by default, configurable as `file_root` in
host.conf). Paths are resolved and any escape (`..`, symlink tricks)
is rejected with `FILE_ERROR {op, reason: "path escapes file root"}`.

### Reserved ranges (do not use without a protocol bump)

| Range     | Reserved for |
|-----------|--------------|
| 0x60–0x6F | audio streaming |
| 0x70–0x7F | screenshots & screen recording |
| 0x80–0x8F | session chat |
| 0x90–0x9F | remote terminal |
| 0xA0–0xAF | Linux quick actions |
| 0xB0–0xBF | multi-user / permissions, biometric lock |
| 0xC0–0xFE | future |
| 0xFF       | DISCONNECT (v1, fixed) |

## Authentication

1. (v2) Client sends `DEVICE_HELLO`.
2. Server replies `AUTH_REQ` with a fresh 16-byte salt and 32-byte nonce.
3. Client derives `key = PBKDF2-HMAC-SHA256(password, salt, 200000)` and
   replies `AUTH_RESP = HMAC-SHA256(key, nonce)`.
4. Server compares against `HMAC-SHA256(stored_key, nonce)` with a
   constant-time compare. Match → `AUTH_OK` (+ device marked trusted);
   mismatch → `AUTH_FAIL` and the connection is closed.

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
