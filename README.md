# Remote

AnyDesk-style remote desktop for Linux — over **Tailscale**. Control your
Linux machine from your Android phone (or another computer). Nothing is
exposed publicly: peers connect directly over the tailnet (WireGuard
encryption), and the app adds its own password gate on top.

- **Linux host** (`remote-host`): streams the screen (X11 or Wayland),
  injects mouse/keyboard, syncs clipboard (text + images), serves file
  transfers, remote terminal, audio, webcam, automation, and a
  camera-for-verification sink. Ships as a `.deb`.
- **Android viewer** (primary): device list, live screen, touch + keyboard,
  clipboard sync, remote file manager. Ships as an `.apk`.
- **Linux viewer** (`remote-viewer`, tkinter): handy for testing.

Wire protocol is documented in [PROTOCOL.md](PROTOCOL.md) (currently v4) —
both viewers speak the same protocol. The roadmap lives in
[FEATURES.md](FEATURES.md).

## Install (host)

Requirements: a Debian/Ubuntu machine with Tailscale installed and logged in
(`tailscale status` shows your 100.x.x.x address), X11 or Wayland desktop
session.

```bash
sudo apt install ./remote_1.0.0_all.deb   # from the GitHub release
sudo remote-set-password                    # set the access password
sudo systemctl enable --now remote-host     # unattended access
```

No Tailscale yet? `sudo yourremote-network-setup` walks you through
installing it and running `tailscale up` (Headscale `--login-server`
supported).

The host listens on TCP **47800** on all interfaces — reachability is
controlled by your Tailscale ACLs / firewall, not by Remote.

Check it's up: `systemctl status remote-host`, or
`ss -tlnp | grep 47800`.

Host files:
- `/etc/remote/host.conf` — salt + derived key (0600). Optional
  `file_root` (default: your home) jails all file-transfer paths;
  optional `fps` (15/30/45/60, default 30) sets the adaptive streamer's
  starting rung.
- `/etc/remote/trusted_devices` — paired devices (trust-on-first-use),
  now with blocklist + time-boxed grants.
- `/etc/remote/device-id` — the host's permanent `YR-XXXX-XXXX` id.
- `/etc/remote/policy.conf` — privacy toggles (see below).
- `/etc/remote/apps.conf`, `/etc/remote/commands.conf` — allowlists for
  `launch-app` and automation `EXEC_RUN` (absent/empty = deny-all).
- `/var/log/remote/connections.log` — connection history.
- `/var/log/remote/remote.jsonl` — structured JSON-lines event log
  (connections, auth, pairing, commands, permission changes).

### Host command-line tools

All installed as `/usr/bin/yourremote-*` symlinks:

- `yourremote-settings` — TUI editor for host.conf, policy.conf, and the
  allowlists.
- `yourremote-network-setup` — Tailscale install + `tailscale up` wizard.
- `yourremote-update` — update checker against GitHub releases.
- `yourremote-pair-code` — issue a single-use 6-digit pairing code
  (optionally as a QR PNG) for the Android pairing flow.

### Pairing a new device

1. On the host: `sudo yourremote-pair-code` (or scan the QR it prints).
2. On the phone: open **Remote** → **Pair** (or add the computer), enter
   the 6-digit code within 10 minutes.
3. The phone then connects with the normal password handshake; the device
   is recorded as trusted.

If `/etc/remote/policy.conf` sets `require_pairing: true`, unknown devices
cannot even start the password handshake until they redeem a code — the
host answers `PAIR_REQUIRED` with its `YR-XXXX-XXXX` id.

### Privacy policy (`/etc/remote/policy.conf`)

```json
{
  "require_approval": false,
  "require_pairing": false,
  "allow_files": true,
  "allow_terminal": true,
  "allow_clipboard": true,
  "allow_audio": true,
  "allow_webcam": true,
  "hide_on_connect": false,
  "idle_disconnect_min": 0
}
```

- `require_approval`: every new session pops a local approval dialog
  (60 s timeout). On a headless host there is no dialog to show — only
  pairing-code sessions get in ("pairing-code mode only").
- `allow_*`: master switches; a disabled feature answers `PERMS_DENIED
  {reason: "disabled by policy"}` regardless of per-device flags.
- `hide_on_connect`: blank the monitor while a client is attached.
- `idle_disconnect_min`: drop sessions idle longer than N minutes
  (0 = never).

Edit with `sudo yourremote-settings` or copy
`/usr/share/doc/remote/examples/policy.conf.example`.

### Unattended access notes

The systemd service runs as the unprivileged `yourremote` user, but screen
capture and input injection need the desktop session. On a single-user
machine, add to
`/etc/systemd/system/remote-host.service.d/override.conf`:

```ini
[Service]
Environment=DISPLAY=:0
Environment=XAUTHORITY=/home/YOURUSER/.Xauthority
```

then `sudo systemctl daemon-reload && sudo systemctl restart remote-host`.
Alternatively, run `remote-host` directly as your desktop user inside the
graphical session. The unit is `Type=notify` with `WatchdogSec=60` — the
host sends `READY=1` once its socket is bound and `WATCHDOG=1` every 30 s.

## Install (Android viewer)

1. On your phone, install Tailscale and log in to the **same tailnet**.
2. Install the APK from the GitHub release:
   `adb install remote-viewer.apk`, or download it on the phone and open it
   (allow "install unknown apps" for your browser/files app).
3. Open **Remote** → **My Computers** → add your Linux box
   (`tailscale ip -4` on the host, e.g. `100.x.x.20`), port `47800`,
   and the password you set.
4. Tap a computer to connect. Tap = left click, drag = move the mouse,
   long-press = right-click. Use the **Keyboard** button for typing
   (a physical keyboard works too).
5. The **Files** button opens the remote file manager (browse, download,
   upload, progress + resume). Clipboard syncs both ways automatically
   (toggle in Settings).

## Security notes

- Transport encryption: Tailscale/WireGuard. Remote never listens on the
  public internet in this setup.
- Access control: PBKDF2-HMAC-SHA256 (200k rounds) challenge-response;
  the host stores only a salt + derived key, never the password.
- Pairing: devices identify with `DEVICE_HELLO`; a 6-digit single-use code
  (`yourremote-pair-code`) marks them trusted pre-auth. Per-IP rate
  limiting with backoff throttles password guessing; blocked devices are
  refused before auth.
- Permissions: twelve fail-closed flags per device (`view, mouse,
  keyboard, clipboard, files, terminal, system, audio, webcam, apps,
  automation, camera`), enforced before handling; policy.conf can disable
  whole features host-wide.
- Session tokens are issued post-auth and rotated hourly, bound to the
  live connection.
- File transfer is jailed: every path is resolved under `file_root`
  (default `$HOME`); `..` escapes and symlink tricks are rejected.
- The terminal never runs a raw shell: `SYSTEM_CMD` is a strict allowlist
  and `EXEC_RUN` only runs `commands.conf` entries.
- The `.deb` postinst creates the unprivileged `yourremote` user,
  `/etc/remote` (0700), `/var/log/remote`, and does **not** enable the
  service until you set a password.

## X11 vs Wayland

X11 capture uses `mss`. On Wayland, the host performs a real
xdg-desktop-portal ScreenCast handshake (in-process D-Bus client +
GStreamer `pipewiresrc`) when the portal, session bus, and GStreamer are
present — otherwise it reports honestly that Wayland capture is
unavailable instead of faking frames.

## Building from source

```bash
# .deb
bash packaging/deb/build-deb.sh        # -> out/remote_*_all.deb

# Android APK (needs JDK 17 + Android SDK build-tools 34, no Gradle)
bash android/build-apk.sh              # -> out/remote-viewer.apk

# self-test (host self-test + viewer client + protocol harnesses v1/v2/v3
#            + every v4 module conformance harness)
bash tests/selftest.sh
```

Releases: pushing a tag `v*` builds both artifacts in GitHub Actions and
attaches `remote_*_all.deb` + `remote-viewer.apk` to the GitHub release.

## What v1.0.0 does NOT do yet

- Android audio playback UI (host-side capture is real; the player ships
  in the next Android release)
- Android camera capture side (host-side `/dev/video0` sink is real;
  the phone capture ships in the next Android release)
- Voice RTX (protocol range 0x90–0x97 reserved)
- iOS viewer

See [FEATURES.md](FEATURES.md) for the full roadmap and the reserved
protocol ranges each future feature will use.
