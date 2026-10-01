# Remote

AnyDesk-style remote desktop for Linux — over **Tailscale**. Control your
Linux machine from your Android phone (or another computer). Nothing is
exposed publicly: peers connect directly over the tailnet (WireGuard
encryption), and the app adds its own password gate on top.

- **Linux host** (`remote-host`): streams the screen, injects mouse/keyboard,
  syncs clipboard, serves file transfers. Ships as a `.deb`.
- **Android viewer** (primary): device list, live screen, touch + keyboard,
  clipboard sync, remote file manager. Ships as an `.apk`.
- **Linux viewer** (`remote-viewer`, tkinter): handy for testing.

Wire protocol is documented in [PROTOCOL.md](PROTOCOL.md) (currently v2) —
both viewers speak the same protocol. The roadmap lives in
[FEATURES.md](FEATURES.md).

## Install (host)

Requirements: a Debian/Ubuntu machine with Tailscale installed and logged in
(`tailscale status` shows your 100.x.x.x address), X11 desktop session.

```bash
sudo apt install ./remote_0.2.0_all.deb   # from the GitHub release
sudo remote-set-password                    # set the access password
sudo systemctl enable --now remote-host     # unattended access
```

The host listens on TCP **47800** on all interfaces — reachability is
controlled by your Tailscale ACLs / firewall, not by Remote.

Check it's up: `systemctl status remote-host`, or
`ss -tlnp | grep 47800`.

Host files:
- `/etc/remote/host.conf` — salt + derived key (0600). Optional
  `file_root` (default: your home) jails all file-transfer paths.
- `/etc/remote/trusted_devices` — paired devices (trust-on-first-use).
- `/var/log/remote/connections.log` — connection history.

### Unattended access notes

The systemd service runs as root, but screen capture and input injection
need the desktop session. On a single-user machine, add to
`/etc/systemd/system/remote-host.service.d/override.conf`:

```ini
[Service]
Environment=DISPLAY=:0
Environment=XAUTHORITY=/home/YOURUSER/.Xauthority
```

then `sudo systemctl daemon-reload && sudo systemctl restart remote-host`.
Alternatively, run `remote-host` directly as your desktop user inside the
graphical session.

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
- Pairing: devices identify with `DEVICE_HELLO` and are recorded in
  `/etc/remote/trusted_devices` on first successful password auth.
- File transfer is jailed: every path is resolved under `file_root`
  (default `$HOME`); `..` escapes and symlink tricks are rejected.
- The `.deb` postinst creates `/etc/remote` (0700) and does **not**
  enable the service until you set a password.

## X11 vs Wayland

Capture is via X11 (`mss`). On Wayland sessions, run the host under XWayland
or an X11 session. Native Wayland capture (PipeWire portal) is planned.

## Building from source

```bash
# .deb
bash packaging/deb/build-deb.sh        # -> out/remote_*_all.deb

# Android APK (needs JDK 17 + Android SDK build-tools 34, no Gradle)
bash android/build-apk.sh              # -> out/remote-viewer.apk

# self-test (host self-test + viewer client + protocol harness v1/v2)
bash tests/selftest.sh
```

Releases: pushing a tag `v*` builds both artifacts in GitHub Actions and
attaches `remote_*_all.deb` + `remote-viewer.apk` to the GitHub release.

## What v2 does NOT do yet

- Wayland native capture (X11 only)
- H.264/video-codec streaming (JPEG frames for now)
- Audio streaming, screenshots/recording, remote terminal
- Multi-user permissions, explicit pair approval, biometric lock
- iOS viewer

See [FEATURES.md](FEATURES.md) for the full roadmap and the reserved
protocol ranges each future feature will use.
