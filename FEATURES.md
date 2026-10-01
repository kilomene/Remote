# Remote — Feature Roadmap

Remote is an AnyDesk-style remote desktop for Linux. Transport runs over
**Tailscale** (WireGuard-encrypted P2P); the app adds password auth,
pairing, and the feature protocol on top. Linux host ships as a `.deb`,
the viewer as a native Android app (APKs on GitHub Releases).

## MVP — the 8 core features (v2, shipped)

1. **Live screen** — X11 capture (mss), JPEG frames ~12fps, changed-frames only.
2. **Touch/mouse** — tap/drag/long-press/two-finger/pinch mapped to
   move/click/scroll input events.
3. **Keyboard** — on-screen + physical, per PROTOCOL.md key names.
4. **Tailscale connection** — direct TCP to the host's tailnet IP, no relay.
5. **Device pairing** — `DEVICE_HELLO` on connect; trust-on-first-use via
   password into `/etc/remote/trusted_devices`; connection history in
   `/var/log/remote/connections.log`. (Explicit accept/reject approval is
   deferred — the host is headless.)
6. **Clipboard sync** — `CLIPBOARD_SET` both directions, text only,
   echo-loop guarded. Host polls X11 (xclip/xsel).
7. **File transfer** — `FILE_*` messages: list/get/put/mkdir/delete/rename,
   256KB chunks, resume via offset, progress = bytes/total. Host paths are
   jailed to the user's home (configurable `file_root`).
8. **Reconnection** — viewer auto-reconnects with exponential backoff;
   host survives client drops and serves sequentially.

## v3 — shipped (host side, Linux)

9.  **Linux quick actions** — `SYSTEM_CMD` strict allowlist (lock, logout,
    reboot, shutdown, suspend, open-terminal, open-browser, open-app,
    blank-screen, switch-display, service); never a raw shell; `service`
    only touches `monitored_services` from host.conf.
10. **Remote terminal** — pty-backed shell channel (`TERMINAL_*`), up to
    8 concurrent sessions, cleaned up on disconnect.
11. **Agent / automation status** — `AGENT_QUERY`/`AGENT_STATUS`: CPU, memory,
    disk, network rates (stdlib `/proc` only) + monitored systemd services.
12. **Session chat** — `CHAT_MSG` broadcast to other sessions + JSON-lines
    chat log on the host. Real broadcast, real persistence — no stub.
13. **Display listing** — `DISPLAYS_QUERY`/`DISPLAYS_LIST` parsed from
    `xrandr` (synthetic fallback when headless).
14. **Multi-user / permissions** — `PERMS_*` (0x80–0x84): seven boolean flags
    per trusted device (`view, mouse, keyboard, clipboard, files, terminal,
    system`), fail-closed enforcement *before* handling (violations get
    `PERMS_DENIED` and are dropped), no-view devices get no frames, a device
    cannot change its own permissions, changes apply live and persist.

## v3 — shipped (Android viewer)

Every control performs the real action against the real protocol/host —
placebo UI was cut, not shipped.

**Session**
- 3 control modes: touch (tap/drag/long-press/two-finger scroll, as v2),
  trackpad (relative virtual cursor, sensitivity-scaled), mouse (visible
  pointer + on-screen L/R buttons); precision toggle slows the cursor.
- Pinch zoom 1–4x + pan when zoomed (double-tap resets); fit/original
  scaling; fullscreen toggle; landscape/portrait/auto orientation.
- Quality presets (Ultra/High/Balanced/Low/Min-latency) + FPS 15/30/45/60 —
  these are *viewer-side* decode settings (fps cap, smoothing, render
  scale). Adaptive mode drops render scale on sustained PONG latency
  >300ms and shows a badge (host-side adaptive bitrate is planned).
- Full keyboard toolbar: sticky Ctrl/Alt/Shift/Super, Esc/Tab/Del/Home/End/
  PgUp/PgDn/arrows, F1–F12, Ctrl combos, customizable shortcut bar
  (Settings), Linux quick buttons (open-terminal, lock).
- Displays switcher from `DISPLAYS_LIST` (needs v3 host, else honest toast);
  privacy toggle via `blank-screen`.
- Connection panel: "Transport: Tailscale (WireGuard)" + endpoint, measured
  PING/PONG latency, protocol version, bytes transferred, reconnect-now.
  (No fake Direct-P2P/Relay indicators — the app cannot determine those.)
- Session timeout actually disconnects; session start/end recorded to
  SQLite with rx/tx bytes; trusted-device pairing marked on auth success;
  connect/disconnect notifications (toggleable).

**Features**
- Screenshots: real PNG of the current frame saved to the gallery
  (MediaStore) with share; history list.
- Screen recording: real playable MP4 (MediaCodec AVC + MediaMuxer),
  pause/resume produces a gapless file (frame timestamps only advance on
  fed frames), published to the gallery; history list.
- Remote terminal: real host pty (`TERMINAL_*`), tabbed, ANSI colors,
  soft-keyboard input, special-key row; `journalctl -u <svc>` from the
  agent dashboard opens a live log tab.
- Agent dashboard: real `/proc` gauges (CPU/RAM/disk/net, 5s refresh) and
  service list; Start/Stop/Restart execute the allowlisted
  `SYSTEM_CMD "service" {name, action}` with confirm dialogs.
- Multi-user permissions: `PERMS_LIST`/`PERMS_SET` against the host;
  7 flags per trusted device; own-device row disabled (host rejects
  self-changes); `PERMS_DENIED` surfaces as "Blocked by host permissions".
  Older hosts get an honest "not supported" message.
- Linux quick actions: allowlisted `SYSTEM_CMD` with confirm dialogs for
  destructive ones; results toasted from `SYSTEM_RESP`.
- Session chat: real `CHAT_MSG` send/receive with unread indicator.
- Clipboard history: last 20 entries, tap to re-send to the host.
- File manager: move (rename across paths), download/upload queue on top
  of the single-lane client, transfer history + notifications.
- Device list: add/edit/remove, tags, icon choice, pin, harvested
  resolution/last-seen/IP metadata; online→offline transitions notify
  while the app is open.
- Biometric app lock: framework `BiometricPrompt` (API 29+, hidden below),
  verified-before-enabling, actually gates app entry.
- Security center: biometric toggle, session-timeout picker, trusted-device
  list with revoke (local-only; host-side revoke is planned), security
  event log (auth failures, revokes).
- Settings sections: Connection (quality, fps, auto-reconnect),
  Controls (default mode, sensitivity, precision), Display (scaling,
  keep-awake, smoothing), Clipboard, Security (biometric, timeout, security
  center), Notifications (session/transfer/offline toggles), Shortcuts
  (custom bar editor).

## Planned — remaining sections (NOT shipped)

| # | Feature | Protocol range | Notes |
|---|---------|----------------|-------|
| 15 | Audio streaming | 0x70–0x7F | PLANNED — host loopback capture is the hard part; range reserved only |
| 20 | Connection-quality stats | — | RTT shown live; loss/jitter + host-side adaptive bitrate planned |
| 21 | LAN discovery / WebRTC | 0x90–0x9F | future transport option alongside Tailscale |
| — | Host-side trust revoke | — | client-side revoke is local-only; host-side revoke planned |

The old v3-planned items 10 (screenshots), 11 (screen recording), 17
(notifications), 18 (session history UI), 19 (biometric lock) shipped in
the Android v3 viewer above.

## Architecture rules (so the above needs no redesign)

- **Message-type registry with reserved ranges** — PROTOCOL.md assigns every
  future feature a dedicated range up front. Adding a feature = filling in
  its range, never renumbering.
- **Host handler registry** — `HostServer.handlers` maps one message type to
  one function (`host/remote_host.py`). New features add a handler function
  (+ a module like `host/file_transfer.py`); the connection loop is untouched.
- **Android feature modules** — `com.remote.viewer.features.*` packages
  (`pairing`, `clipboard`, `files`, …). Each owns its wire messages and UI;
  `RemoteClient` only dispatches.
- **Capability advertisement** — `AUTH_OK` carries `REMOTE/N`. Clients gate
  features on the version; unknown versions degrade to the v1 subset.
- **Jail everything by default** — new host-side capabilities (terminal,
  quick actions) must follow the file-transfer pattern: an explicit allow
  list / sandbox, never ambient authority.
