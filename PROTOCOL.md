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
- **v3** (`AUTH_OK` payload `b"REMOTE/3"`): v2 plus whitelisted system
  commands, pty-backed remote terminal, agent status, display listing,
  session chat, and enforced multi-user permissions (0x80–0x84).

All v1 message types are byte-identical in v2 and v3. Clients must accept any
`AUTH_OK` payload starting with `b"REMOTE/"` and enable features based on
the version number. A v1 client talking to a v3 host simply never sends
v2/v3 messages; a v3 client talking to a v1 host must degrade gracefully
(no response to v2/v3 messages means "not supported").

## Message types

### v1 (unchanged)

| Type | Name         | Dir            | Payload |
|------|--------------|----------------|---------|
| 0x01 | AUTH_REQ     | server → client | `salt(16) \|\| nonce(32)` |
| 0x02 | AUTH_RESP    | client → server | `HMAC-SHA256(key, nonce)` (32 bytes) |
| 0x03 | AUTH_OK      | server → client | `b"REMOTE/1"`, `b"REMOTE/2"` or `b"REMOTE/3"` |
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
| 0x60–0x6E | v3: system commands, terminal, chat, agent status, displays (0x69–0x6E spare) |
| 0x6F      | v3: SYSTEM_RESP |
| 0x70–0x7F | audio streaming (PLANNED — host loopback capture is the hard part) |
| 0x80–0x84 | multi-user / permissions (used, see below) |
| 0x85–0x8F | reserved (multi-user extensions) |
| 0x90–0x9F | WebRTC / STUN / TURN |
| 0xC0–0xFE | future |
| 0xFF      | DISCONNECT (v1, fixed) |

### v3 — system commands (0x60 / 0x6F)

| Type | Name         | Dir            | Payload |
|------|--------------|----------------|---------|
| 0x60 | SYSTEM_CMD   | client → server | UTF-8 JSON `{cmd, args?}` |
| 0x6F | SYSTEM_RESP  | server → client | UTF-8 JSON `{cmd, ok, detail?}` |

STRICT allowlist — never a raw shell. Anything not on the list is rejected
with `{cmd, ok: false, detail: "not allowed"}`:

- `lock` → `loginctl lock-session`, falling back to `xdg-screensaver lock`
- `logout` → `loginctl terminate-user $USER`
- `reboot` / `shutdown` / `suspend` → `systemctl reboot` / `poweroff` / `suspend`
- `open-terminal` → first available of
  `x-terminal-emulator`, `gnome-terminal`, `konsole`, `xterm`
- `open-browser` `{url}` → url must start with `http://` or `https://`,
  then `xdg-open <url>`
- `open-app` `{name}` → strict map
  (`terminal`/`files`/`browser`/`vscode` → fixed `.desktop` allowlist);
  unknown name → reject
- `blank-screen` `{on: bool}` → `xset dpms force off` / `force on`
- `switch-display` `{id}` → id validated against the `xrandr` output,
  then `xrandr --output <name> --primary`
- `service` `{name, action}` → name must be in `monitored_services` from
  `host.conf` (default `["remote-host", "tailscaled"]`), action in
  `{start, stop, restart}` → `systemctl <action> <name>`

All commands run detached (`Popen`, `start_new_session=True`) and never use
`shell=True` where user data is involved.

### v3 — remote terminal (0x61–0x63)

| Type | Name           | Dir            | Payload |
|------|---------------|----------------|---------|
| 0x61 | TERMINAL_OPEN   | client → server | UTF-8 JSON `{cols, rows}` |
| 0x61 | TERMINAL_OPENED | server → client | UTF-8 JSON `{session}` (same type id; direction distinguishes) |
| 0x62 | TERMINAL_DATA   | either          | UTF-8 JSON `{session, data}` — `data` is base64 of raw pty bytes |
| 0x63 | TERMINAL_CLOSE  | either          | UTF-8 JSON `{session}` |

The host opens a login shell on a fresh pty per `TERMINAL_OPEN` and pumps
pty output back as `TERMINAL_DATA`. Client keystrokes arrive as
`TERMINAL_DATA` and are written to the pty master. Either side may close;
the host also closes the session's process group when the client
disconnects. Concurrent sessions are capped at 8 (further opens get
`TERMINAL_OPENED {session: 0, error}`).

### v3 — session chat (0x64)

| Type | Name     | Dir    | Payload |
|------|----------|--------|---------|
| 0x64 | CHAT_MSG | either | UTF-8 JSON `{from, text, ts}` |

A client's message is broadcast to all *other* sessions and appended as a
JSON line to the host chat log (`/var/log/remote/chat.log`).

### v3 — agent status (0x65–0x66)

| Type | Name         | Dir            | Payload |
|------|--------------|----------------|---------|
| 0x65 | AGENT_QUERY  | client → server | UTF-8 JSON `{}` (or `{services: [...]}` to override the monitored list) |
| 0x66 | AGENT_STATUS | server → client | UTF-8 JSON `{cpu_pct, mem_total_mb, mem_used_mb, disk_total_gb, disk_used_gb, net_rx_bps, net_tx_bps, services: [{name, active}], ts}` |

### v3 — displays (0x67–0x68)

| Type | Name           | Dir            | Payload |
|------|---------------|----------------|---------|
| 0x67 | DISPLAYS_QUERY | client → server | empty or `{}` |
| 0x68 | DISPLAYS_LIST  | server → client | UTF-8 JSON `{displays: [{id, name, w, h, primary}], active}` |

Parsed from `xrandr --query`; if xrandr is missing, a single display is
synthesized from the capture size.

### v3 — multi-user / permissions (0x80–0x84), enforced by the host

| Type | Name            | Dir            | Payload |
|------|-----------------|----------------|---------|
| 0x80 | PERMS_SET       | client → server | UTF-8 JSON `{device_id, permissions: {view, mouse, keyboard, clipboard, files, terminal, system}}` |
| 0x81 | PERMS_RESP      | server → client | UTF-8 JSON `{device_id, ok, detail?}` |
| 0x82 | PERMS_DENIED    | server → client | UTF-8 JSON `{op, reason}` |
| 0x83 | PERMS_LIST      | client → server | `{}` |
| 0x84 | PERMS_LIST_RESP | server → client | UTF-8 JSON `{devices: [{device_id, device_name, platform, first_seen, last_seen, permissions}]}` |

Every trusted device carries all seven permission flags (booleans).
Trust-on-first-use defaults them all to true. Rules:

- `PERMS_SET` target must exist in the trust store; all seven flags are
  required and must be booleans, otherwise `ok: false`.
- A device **cannot change its own permissions**
  (`ok: false, detail: "cannot change own permissions"`).
- Changes apply to live sessions immediately and are persisted in the
  trust store.

Enforcement is fail-closed and checked *before* handling each message; a
violation gets `PERMS_DENIED {op, reason}` and the message is dropped:

| Message | Required flag |
|---------|---------------|
| INPUT `move`/`click`/`scroll` | `mouse` |
| INPUT `key` | `keyboard` |
| CLIPBOARD_SET | `clipboard` |
| FILE_* | `files` |
| TERMINAL_* | `terminal` |
| SYSTEM_CMD | `system` |
| FRAME stream | `view` (the frame loop only starts with `view: true`) |

Always allowed for authenticated clients: PING/PONG, DISCONNECT, CHAT_MSG,
AGENT_QUERY, DISPLAYS_QUERY, PERMS_*.

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
