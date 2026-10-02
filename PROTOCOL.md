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
- **v4** (`AUTH_OK` payload `b"REMOTE/4"`): v3 plus remote audio
  streaming, webcam frames, Tailscale net status, pairing approval
  (0x77–0x79), allowlisted automation exec, policy toggles, terminal
  resize (0x69), session tokens (0x85–0x86), camera for verification
  (0x87–0x8A), and twelve permission
  flags.

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
| 0x60–0x6E | v3: system commands, terminal, chat, agent status, displays (0x69 = TERMINAL_RESIZE) |
| 0x6F      | v3: SYSTEM_RESP |
| 0x70–0x73 | v4: remote audio streaming |
| 0x74–0x75 | v4: webcam |
| 0x76–0x79 | v4: net status + pairing |
| 0x7A–0x7D | v4: automation exec + policy toggles (0x7D spare) |
| 0x7E–0x7F | reserved |
| 0x80–0x84 | multi-user / permissions (used, see below) |
| 0x85–0x86 | v4: session tokens (multi-user extension) |
| 0x87–0x8A | v4: camera for verification |
| 0x8B–0x8F | reserved (multi-user extensions) |
| 0x90–0x97 | voice RTX (planned) |
| 0x98–0x9F | reserved (remainder of the old WebRTC/STUN/TURN block) |
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

## v4 (REMOTE/4)

`AUTH_OK` payload is `b"REMOTE/4"`. All v1/v2/v3 types are byte-identical;
clients still enable features from the version number in the `AUTH_OK`
payload.

### v4 — terminal resize (0x69, fills the v3 spare slot)

| Type | Name            | Dir            | Payload |
|------|-----------------|----------------|---------|
| 0x69 | TERMINAL_RESIZE | client → server | UTF-8 JSON `{session, cols, rows}` |

Asks the host to resize the pty window for the given terminal session
(session id from `TERMINAL_OPENED`). No response; the next
`TERMINAL_DATA` reflects the new size.

### v4 — remote audio (0x70–0x73)

| Type | Name        | Dir            | Payload |
|------|-------------|----------------|---------|
| 0x70 | AUDIO_START | client → server | UTF-8 JSON `{source}` |
| 0x71 | AUDIO_DATA  | server → client | raw audio bytes |
| 0x72 | AUDIO_STOP  | either          | empty |
| 0x73 | AUDIO_ERROR | server → client | UTF-8 JSON `{detail}` |

Flow: client sends `AUDIO_START {source}` (`source` names the capture
source the client wants, e.g. the host's audio monitor). The host streams
`AUDIO_DATA` chunks until either side sends `AUDIO_STOP`. A failure to
start or a capture error mid-stream gets `AUDIO_ERROR {detail}`. One
active stream per session; a second `AUDIO_START` replaces the first.

### v4 — webcam (0x74–0x75)

| Type | Name         | Dir            | Payload |
|------|--------------|----------------|---------|
| 0x74 | WEBCAM_LIST  | client → server | UTF-8 JSON `{}` |
| 0x74 | WEBCAM_LIST  | server → client | UTF-8 JSON `{cameras: [{id, name}]}` (same type id; direction distinguishes) |
| 0x75 | WEBCAM_FRAME | client → server | UTF-8 JSON `{id}` |
| 0x75 | WEBCAM_FRAME | server → client | JPEG bytes (same type id; direction distinguishes) |

The client discovers cameras with `WEBCAM_LIST` (an empty `id` list means
no camera is present on the host), then requests single frames with
`WEBCAM_FRAME {id}`. The server replies with a complete JPEG frame
(starts `FF D8`, ends `FF D9`), like `FRAME`. Polling is client-driven;
there is no unsolicited push. A failed capture gets `WEBCAM_FRAME` with a
UTF-8 JSON `{error}` payload instead — the client branches on the first
bytes (JPEG starts `FF D8`).

### v4 — Tailscale net status (0x76)

| Type | Name       | Dir            | Payload |
|------|------------|----------------|---------|
| 0x76 | NET_STATUS | client → server | UTF-8 JSON `{}` |
| 0x76 | NET_STATUS | server → client | UTF-8 JSON `{online, tailscale_ip, peer_latency_ms, direct}` (same type id; direction distinguishes) |

Fields:

| Field | Type | Meaning |
|-------|------|---------|
| online | bool | host can reach the Tailscale network |
| tailscale_ip | string | host's Tailscale IPv4 (e.g. `"100.80.146.11"`), or `""` when offline |
| peer_latency_ms | number | measured RTT to this viewer, or `-1` if unknown |
| direct | bool | true when the path is direct peer-to-peer (not relayed) |

### v4 — pairing approval (0x77–0x79)

| Type | Name          | Dir            | Payload |
|------|---------------|----------------|---------|
| 0x77 | PAIR_REQUEST  | client → server | UTF-8 JSON `{code, device_id, device_name, platform}` |
| 0x78 | PAIR_RESULT   | server → client | UTF-8 JSON `{ok, detail?}` |
| 0x79 | PAIR_REQUIRED | server → client | UTF-8 JSON `{device_id}` |

All three are sent **pre-auth**, after `DEVICE_HELLO` and before the
auth challenge/response. Codes are single-use, issued out-of-band by the
host owner (shown on the host or encoded in a QR PNG).

Pairing flow:

1. Client sends `DEVICE_HELLO`, then `PAIR_REQUEST` with the pairing
   code and its device identity.
2. Host validates the code and replies `PAIR_RESULT {ok: true}`; the
   device is recorded in the trust store. On failure the host replies
   `{ok: false, detail: "..."}` and closes the connection.
3. The normal auth handshake follows (`AUTH_REQ` / `AUTH_RESP` /
   `AUTH_OK`).

When host policy `require_pairing` is true and an **unknown** device
connects without a valid `PAIR_REQUEST`, the host answers
`PAIR_REQUIRED {device_id}` instead of `AUTH_REQ` — `device_id` is the
*host's* `YR-XXXX-XXXX` id, so the client can show which machine demands
pairing. The client must surface a pairing prompt; no auth challenge is
issued until pairing completes. Known/trusted devices are unaffected.

### v4 — automation exec (0x7A–0x7B)

| Type | Name        | Dir            | Payload |
|------|-------------|----------------|---------|
| 0x7A | EXEC_RUN    | client → server | UTF-8 JSON `{name, args: []}` |
| 0x7B | EXEC_RESULT | server → client | UTF-8 JSON `{name, ok, output, error?}` |

`name` selects a command from the host's strict allowlist registry —
never a raw shell. `args` is an array of strings passed to the
allowlisted command. The host replies `EXEC_RESULT` with `ok: true` and
`output` (UTF-8, possibly truncated), or `ok: false` with `error?`
(e.g. `"not allowed"`, `"failed: <reason>"`).

### v4 — privacy policy toggles (0x7C)

| Type | Name       | Dir            | Payload |
|------|------------|----------------|---------|
| 0x7C | POLICY_GET | client → server | UTF-8 JSON `{}` |
| 0x7C | POLICY_GET | server → client | UTF-8 JSON `{toggles}` (same type id; direction distinguishes) |

`toggles` is an object mapping policy toggle names to booleans, mirroring
the host's privacy policy (`policy.conf`). Viewers use it to gray out
features the host has disabled. `0x7D` is spare in the v4 policy block.

### v4 — session tokens (0x85–0x86)

| Type | Name           | Dir            | Payload |
|------|---------------|----------------|---------|
| 0x85 | SESSION_TOKEN  | server → client | UTF-8 JSON `{token, expires_in}` |
| 0x86 | SESSION_ROTATE | server → client | UTF-8 JSON `{token, expires_in}` |

Lifecycle:

- Issued once, immediately after `AUTH_OK`, as `SESSION_TOKEN`
  `{token: <opaque string>, expires_in: <seconds>}`.
- Rotated hourly: the host sends `SESSION_ROTATE` with a fresh token;
  the previous token expires at once.
- Design decision: tokens are **single-connection nonces**. They are
  informational and bound to the TCP connection they were issued on —
  **replaying a token on a new connection is rejected** (the host's
  trust store maps each token to its originating connection). A
  reconnecting client completes the full auth handshake and receives a
  new token.
- The host never accepts a token as a substitute for auth.

### v4 — clipboard images (0x40, extended)

`CLIPBOARD_SET` is no longer text-only. The payload is UTF-8 JSON
`{text}` (unchanged from v2) **or** `{mime, data}` for images:

| Field | Meaning |
|-------|---------|
| mime | image MIME type, e.g. `"image/png"` |
| data | base64-encoded image bytes |

Both sides keep the v2 echo-suppression rule (ignore a change that
matches the last text or image it sent or received).

### v4 — permission flags (12 total)

Every trusted device now carries twelve permission flags (booleans):

`view, mouse, keyboard, clipboard, files, terminal, system,`
`audio, webcam, apps, automation, camera`

The v3 rules still apply (`PERMS_SET` requires all flags as booleans, a
device cannot change its own permissions, changes apply to live sessions
immediately). New v4 enforcement rows — fail-closed, checked before
handling each message, violations get `PERMS_DENIED {op, reason}`:

| Message | Required flag |
|---------|---------------|
| AUDIO_* | `audio` |
| WEBCAM_* | `webcam` |
| EXEC_* | `automation` |
| CAMERA_* | `camera` |

### v4 — camera for verification (0x87–0x8A)

The user's Android phone has the camera; the Linux host may not. On an
explicit `CAMERA_START` the phone streams H264 camera frames over the
encrypted session and the host presents them to Linux apps as a real
virtual camera (`/dev/videoN`, dynamically selected — `/dev/video0` is
never assumed) via v4l2loopback. Verification-only: consent-gated, never
auto-starts, never records, terminates cleanly.

| Type | Name          | Dir            | Payload |
|------|---------------|----------------|---------|
| 0x87 | CAMERA_START  | client → server | UTF-8 JSON `{width, height, fps, facing, rotation?, mirror?, codec?}` — `facing` is `"front"` or `"rear"`; `rotation` is `0`/`90`/`180`/`270` (degrees the host must rotate frames to make them upright); `mirror` is a bool (front cameras usually mirror); `codec` is `"h264"` |
| 0x88 | CAMERA_STOP   | either          | empty |
| 0x89 | CAMERA_FRAME  | client → server | raw H264 **Annex-B** bytes — one access unit per message (≤ 8 MiB, the global payload cap). The payload MUST begin with an Annex-B start code; anything else is rejected |
| 0x8A | CAMERA_STATUS | server → client | UTF-8 JSON `{active, device, width, height, fps, error?}` — `device` is the selected `/dev/videoN`; `width`/`height` are the presented (post-rotation) geometry |

Wire rules for the Android implementer:

- `CAMERA_START` is the ONLY way the camera turns on. There is no
  auto-start on connect, and the host never requests it unprompted.
- `width`/`height`/`fps` are integers describing the ENCODED stream
  geometry; the host accepts 160–3840 × 120–2160 @ 1–60 fps. `rotation`
  and `mirror` default to `0`/`false` when absent (backward compatible).
  The host rotates/mirrors in its ffmpeg filter chain
  (`transpose`/`hflip`) and echoes the accepted presented geometry back
  in `CAMERA_STATUS`.
- `CAMERA_FRAME` carries one access unit per message: a complete
  Annex-B NALU sequence (start codes `0x000001` / `0x00000001` between
  NALUs) — typically one IDR or one non-IDR frame's NALUs plus any
  prefix SEI/SPS/PPS. Do NOT split an access unit across messages and do
  NOT bundle multiple access units into one message.
- **SPS/PPS in-band**: the first `CAMERA_FRAME` after `CAMERA_START`
  MUST begin with the SPS and PPS NALUs (or carry them prefixing the
  first IDR), and they MUST be re-sent in-band whenever the encoder
  restarts, changes parameters, or at least once every few seconds
  (the host does not keep decoder state across a reconnect). The
  Android implementation prepends SPS/PPS to every IDR.
- The host pipes each payload into
  `ffmpeg -f h264 -i pipe:0 -pix_fmt yuv420p -vf <transpose/hflip/scale> -r FPS -f v4l2 /dev/videoN`.
  Encoded stream parameters must match the `CAMERA_START` geometry.
- Device selection: the host first reuses an existing v4l2loopback node
  carrying the `YourRemote` card label; otherwise it loads v4l2loopback
  with `video_nr=-1` (the kernel picks a free number) and adopts the new
  node. On stop it best-effort unloads the module only if it loaded it.
  At startup the host kills orphaned `ffmpeg ... -f v4l2` writers left by
  a crashed previous instance, so no stale virtual-camera processes
  remain.
- The host emits `CAMERA_STATUS` after every state change: start
  accepted (`{active: true, device: "/dev/videoN", ...}`), start
  rejected or mid-stream failure (`{active: false, error: "..."}`),
  explicit `CAMERA_STOP` (`{active: false, ...}`), and inactivity
  auto-stop (`{active: false, error: "stopped: inactivity timeout"}`).
  `error` is present only when something failed.
- The host requires the `camera` permission flag (fail-closed, checked
  before handling any `CAMERA_*` message; violations get
  `PERMS_DENIED {op: "CAMERA_START", reason: "..."}` and the message is
  dropped). Only ONE camera session is active per host: a second
  `CAMERA_START` while active is answered with
  `CAMERA_STATUS {active: false, error: "camera already in use"}`.
- Inactivity: if no `CAMERA_FRAME` arrives for 60 s the host auto-stops
  and releases `/dev/video0` immediately — the client does not need to
  send `CAMERA_STOP` on pause, but SHOULD send it when the user
  explicitly ends verification so the device releases without delay.
- The client MAY send `CAMERA_STOP` at any time; the host also sends
  `CAMERA_STOP` semantics via `CAMERA_STATUS {active: false}` if it
  tears the session down itself (e.g. on disconnect the host releases
  the device without waiting for a stop message).
| SYSTEM_CMD `open-app` (app launching) | `apps` |
| SYSTEM_CMD (all others) | `system` |

Always allowed for authenticated clients: PING/PONG, DISCONNECT,
CHAT_MSG, AGENT_QUERY, DISPLAYS_QUERY, NET_STATUS, POLICY_GET,
PERMS_*, SESSION_*.

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
