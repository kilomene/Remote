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

## Planned — the other 13 sections (NOT in MVP)

| # | Feature | Protocol range | Notes |
|---|---------|----------------|-------|
| 9 | Audio streaming | 0x60–0x6F | host mic/speaker loopback → Opus; needs PulseAudio/PipeWire capture |
| 10 | Screenshots | 0x70–0x7F | capture current frame to host disk / pull to viewer |
| 11 | Screen recording | 0x70–0x7F | server-side MP4 (ffmpeg) or client-side frame dump |
| 12 | Multi-user / permissions | 0xB0–0xBF | per-device capability grants (view-only vs control vs files) |
| 13 | Session chat | 0x80–0x8F | text messages between viewer and host-side notifier |
| 14 | Remote terminal | 0x90–0x9F | pty-backed shell channel |
| 15 | Agent / automation controls | 0x90–0x9F | scripted action playback on host |
| 16 | Linux quick actions | 0xA0–0xAF | lock screen, logout, reboot, volume — whitelisted commands |
| 17 | Notifications | 0xA0–0xAF | host → viewer event pushes |
| 18 | Session history UI | — | viewer-side: render `/var/log/remote/connections.log` |
| 19 | Biometric lock | 0xB0–0xBF | gate the Android app behind fingerprint/face |
| 20 | Connection-quality stats | — | extended RTT/loss/jitter in the session overlay |
| 21 | LAN discovery / WebRTC | — | future transport option alongside Tailscale |

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
