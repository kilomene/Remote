#!/usr/bin/env bash
#
# install.sh — one-command full setup of the Remote Linux host.
#
#   curl -fsSL https://github.com/kilomene/Remote/releases/latest/download/install.sh \
#     | sudo bash
#
# Headless / agent use (no prompts at all):
#   curl -fsSL https://github.com/kilomene/Remote/releases/latest/download/install.sh \
#     -o /tmp/install-remote.sh
#   sudo TS_AUTHKEY=tskey-... REMOTE_PASSWORD=... \
#     bash /tmp/install-remote.sh --yes
#
# What it does, idempotently (safe to re-run):
#   1. verifies Debian/Ubuntu + root
#   2. downloads the latest remote_<ver>_all.deb from GitHub Releases
#      (or uses --deb PATH for a local file)
#   3. installs the .deb with dependency resolution
#   4. installs Tailscale via the yourremote-network-setup wizard
#      (official apt repo when missing) and runs `tailscale up`
#      — interactive auth URL, --authkey/TS_AUTHKEY headless, or
#      --headscale/HEADSCALE_URL for a self-hosted control plane
#   5. installs v4l2loopback-dkms (Camera for Verification)
#   6. sets the host password (REMOTE_PASSWORD/--password headless,
#      interactive prompt otherwise; never echoed or logged)
#   7. enables + starts the remote-host systemd service and verifies it
#   8. prints a status summary (version, Tailscale IP, service, pairing)
#
set -euo pipefail

REPO="kilomene/Remote"
PORT=47800

# ---- options (flags override env) -------------------------------------------
DEB_PATH="${DEB_PATH:-}"
WANT_VERSION="${REMOTE_VERSION:-}"   # e.g. 1.2.0 (leading v stripped)
AUTHKEY="${TS_AUTHKEY:-}"
HEADSCALE="${HEADSCALE_URL:-}"
PASSWORD="${REMOTE_PASSWORD:-}"
ASSUME_YES=0
DRY_RUN=0
NO_SYSTEMD=0
DO_UNINSTALL=0

usage() {
    cat <<'EOF'
Usage: install.sh [options]

Options:
  --deb PATH            install a local .deb instead of downloading
  --version VER         pin a release version (default: latest)
  --authkey KEY         headless Tailscale auth key (env: TS_AUTHKEY)
  --headscale URL       self-hosted Headscale control plane (env: HEADSCALE_URL)
  --password PW         host password, non-interactive (env: REMOTE_PASSWORD;
                        env is preferred: a flag is visible in `ps`)
  --yes, -y             assume yes; never prompt (required for headless)
  --dry-run             print what would be done, change nothing
  --no-systemd          skip systemd enable/start (containers); prints the
                        manual run command instead
  --uninstall           remove the remote package and exit
  --help, -h            this help

Headless example:
  sudo TS_AUTHKEY=tskey-abc REMOTE_PASSWORD=s3cret \
    bash install.sh --yes
EOF
}

# ---- argument parsing --------------------------------------------------------
while [ $# -gt 0 ]; do
    case "$1" in
        --deb)          DEB_PATH="$2"; shift 2;;
        --deb=*)        DEB_PATH="${1#--deb=}"; shift;;
        --version)      WANT_VERSION="$2"; shift 2;;
        --version=*)    WANT_VERSION="${1#--version=}"; shift;;
        --authkey)      AUTHKEY="$2"; shift 2;;
        --authkey=*)    AUTHKEY="${1#--authkey=}"; shift;;
        --headscale)    HEADSCALE="$2"; shift 2;;
        --headscale=*)  HEADSCALE="${1#--headscale=}"; shift;;
        --password)     PASSWORD="$2"; shift 2;;
        --password=*)   PASSWORD="${1#--password=}"; shift;;
        --yes|-y)       ASSUME_YES=1; shift;;
        --dry-run)      DRY_RUN=1; shift;;
        --no-systemd)   NO_SYSTEMD=1; shift;;
        --uninstall)    DO_UNINSTALL=1; shift;;
        --help|-h)      usage; exit 0;;
        *) echo "ERROR: unknown option: $1 (see --help)" >&2; exit 2;;
    esac
done
WANT_VERSION="${WANT_VERSION#v}"   # tolerate "v1.2.0"

# ---- helpers -----------------------------------------------------------------
log()  { echo "== $*" >&2; }  # progress chatter -> stderr; stdout is reserved for data (e.g. resolve_deb's path)
warn() { echo "WARNING: $*" >&2; }
die()  { echo "ERROR: $*" >&2; exit 1; }

# run a command; echo it first. In dry-run mode only echo, never execute.
run() {
    if [ "$DRY_RUN" = 1 ]; then echo "[dry-run] + $*" >&2; return 0; fi
    echo "+ $*" >&2
    "$@"
}

# run_display DISPLAY CMD... — like run(), but echo DISPLAY instead of the
# real argv (for commands carrying secrets: the secret never hits the log).
run_display() {
    local disp="$1"; shift
    if [ "$DRY_RUN" = 1 ]; then echo "[dry-run] + $disp" >&2; return 0; fi
    echo "+ $disp" >&2
    "$@"
}

have() { command -v "$1" >/dev/null 2>&1; }

# ---- step 0: root + distro ----------------------------------------------------
ensure_root() {
    [ "$DRY_RUN" = 1 ] && return 0
    if [ "$(id -u)" -eq 0 ]; then return 0; fi
    if have sudo; then
        log "re-running as root via sudo (env preserved for TS_AUTHKEY etc.)"
        # Secrets can't survive the re-exec on a command line without
        # hitting logs, so promote flag-provided secrets to the env vars
        # the tools read (the tools never print them). export keeps even
        # space-containing values intact; sudo -E preserves the env.
        [ -n "$PASSWORD" ] && export REMOTE_PASSWORD="$PASSWORD"
        [ -n "$AUTHKEY" ] && export TS_AUTHKEY="$AUTHKEY"
        exec sudo -E bash "$0" \
            ${DEB_PATH:+--deb="$DEB_PATH"} \
            ${WANT_VERSION:+--version="$WANT_VERSION"} \
            ${HEADSCALE:+--headscale="$HEADSCALE"} \
            ${ASSUME_YES:+--yes} ${DRY_RUN:+--dry-run} \
            ${NO_SYSTEMD:+--no-systemd} \
            ${DO_UNINSTALL:+--uninstall} "$@"
    fi
    die "must run as root (no sudo found). Fix: run with sudo / as root."
}

check_distro() {
    have apt-get || die "apt-get not found: this installer only supports Debian/Ubuntu. Fix: run on a Debian/Ubuntu host."
    if [ -f /etc/debian_version ]; then return 0; fi
    if [ -f /etc/os-release ] && grep -qiE 'ID_LIKE=.*debian|ID=(debian|ubuntu)' /etc/os-release; then return 0; fi
    die "not a Debian/Ubuntu system (/etc/debian_version missing). This installer only supports apt-based distros."
}

# ---- step 1: resolve the .deb --------------------------------------------------
latest_tag() {
    # prints the latest release tag, e.g. v1.2.1; returns non-zero on failure.
    # NOTE: must NOT die in here — callers capture this via $(...), where
    # die's exit would only kill the subshell and execution would continue
    # with an empty tag (seen live: bogus //remote__all.deb 404).
    have curl || { echo "curl not found (needed to download the .deb). Fix: apt-get install -y curl" >&2; return 1; }
    local tag
    # 1) GitHub API — clean JSON, but it 403s hard on rate-limited/shared IPs.
    tag="$(curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" 2>/dev/null \
        | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -n1)"
    # 2) fallback: follow the /releases/latest redirect on github.com, which
    #    is far more lenient than api.github.com.
    if [ -z "$tag" ]; then
        tag="$(curl -fsSIL -o /dev/null -w '%{url_effective}' \
            "https://github.com/${REPO}/releases/latest" 2>/dev/null \
            | sed -n 's#.*/tag/\([^/]*\)$#\1#p')"
    fi
    [ -n "$tag" ] || return 1
    echo "$tag"
}

resolve_deb() {
    # echoes the path to a usable .deb file
    if [ -n "$DEB_PATH" ]; then
        [ -f "$DEB_PATH" ] || die "--deb file not found: $DEB_PATH"
        echo "$DEB_PATH"
        return 0
    fi
    local tag ver url tmpd deb
    if [ -n "$WANT_VERSION" ]; then
        tag="v$WANT_VERSION"; ver="$WANT_VERSION"
    else
        # die here (main shell), not inside latest_tag: its exit would only
        # kill the $(...) subshell and we'd continue with an empty tag.
        tag="$(latest_tag)" \
            || die "could not determine the latest release from GitHub. Fix: pass --version X.Y.Z or --deb PATH."
        ver="${tag#v}"
    fi
    url="https://github.com/${REPO}/releases/download/${tag}/remote_${ver}_all.deb"
    tmpd="$(mktemp -d)"
    deb="$tmpd/remote_${ver}_all.deb"
    log "downloading $url"
    if [ "$DRY_RUN" = 1 ]; then
        echo "[dry-run] + curl -fsSL $url -o $deb" >&2
        echo "$deb"
        return 0
    fi
    run curl -fsSL "$url" -o "$deb" || die "download failed: $url. Fix: check the version/tag, or pass --deb PATH."
    [ -s "$deb" ] || die "downloaded file is empty: $deb"
    if have ar; then
        ar t "$deb" 2>/dev/null | grep -q "debian-binary" \
            || die "downloaded file is not a Debian package: $url"
    fi
    echo "$deb"
}

deb_installed_version() {
    # prints installed version of the remote package, or empty
    dpkg-query -W -f='${Version}' remote 2>/dev/null || true
}

install_deb() {
    local deb="$1" want_ver="$2"
    local installed deb_ver
    installed="$(deb_installed_version)"
    deb_ver=""
    if have dpkg-deb; then
        deb_ver="$(dpkg-deb -f "$deb" Version 2>/dev/null || true)"
    fi
    # Skip only when the installed version is at least as new as the deb we
    # are about to install. A "latest" run with an older version installed
    # upgrades instead of silently skipping (seen live: 1.2.1 kept while
    # the installer downloaded 1.2.4).
    if [ -n "$installed" ] && [ -n "$deb_ver" ] \
        && dpkg --compare-versions "$installed" ge "$deb_ver" 2>/dev/null; then
        log "remote $installed already installed — skipping (re-run with --version to change)"
        return 0
    fi
    [ -n "$installed" ] && log "installed version $installed is older than ${deb_ver:-the package} — upgrading"
    export DEBIAN_FRONTEND=noninteractive
    log "installing $deb (dependencies resolved via apt)"
    if ! run apt-get install -y "$deb"; then
        warn "apt-get install failed; falling back to dpkg -i + apt-get install -f"
        run dpkg -i "$deb" || true
        run apt-get install -f -y \
            || die "dependency repair failed. Fix: apt-get install -f -y, then re-run."
    fi
    [ -n "$(deb_installed_version)" ] || [ "$DRY_RUN" = 1 ] \
        || die "package did not end up installed. Fix: dpkg -l remote; apt-get install -f -y"
    [ "$DRY_RUN" = 1 ] || log "remote $(deb_installed_version) installed"
}

# ---- step 2: tailscale (delegates to the wizard — no duplicated logic) ---------
setup_tailscale() {
    have yourremote-network-setup \
        || die "yourremote-network-setup not found after .deb install (broken package?)"
    local args=()
    [ "$ASSUME_YES" = 1 ] && args+=(--yes)
    [ -n "$HEADSCALE" ] && args+=(--headscale "$HEADSCALE")
    if [ -n "$AUTHKEY" ]; then
        log "tailscale: headless auth via auth key"
        run_display "yourremote-network-setup ${args[*]} --authkey <redacted>" \
            yourremote-network-setup "${args[@]}" --authkey "$AUTHKEY"
    else
        if [ "$ASSUME_YES" = 1 ]; then
            log "tailscale: non-interactive, no auth key — wizard will bring up tailscale if already logged in"
        else
            log "tailscale: interactive — the wizard prints a login URL for you to open"
        fi
        run yourremote-network-setup "${args[@]}"
    fi
}

# ---- step 3: v4l2loopback-dkms (Camera for Verification) ------------------------
setup_v4l2loopback() {
    if dpkg -s v4l2loopback-dkms >/dev/null 2>&1; then
        log "v4l2loopback-dkms already installed — skipping"
        return 0
    fi
    export DEBIAN_FRONTEND=noninteractive
    log "installing v4l2loopback-dkms (virtual camera for Camera for Verification)"
    if ! run apt-get install -y v4l2loopback-dkms; then
        warn "could not install v4l2loopback-dkms (camera feature will be unavailable)."
        warn "Fix later with: sudo apt-get install -y v4l2loopback-dkms"
        return 0
    fi
    log "v4l2loopback-dkms installed"
}

# ---- step 4: host password ------------------------------------------------------
# REMOTE_HOST_CONF override exists so the installer is testable without
# touching the real /etc/remote.
HOST_CONF="${REMOTE_HOST_CONF:-/etc/remote/host.conf}"

setup_password() {
    if [ -n "$PASSWORD" ]; then
        log "setting host password (from flag/env — never echoed)"
        # remote-set-password reads REMOTE_PASSWORD from the environment so
        # the secret never appears on a command line (ps) or in this log.
        run_display "REMOTE_PASSWORD=<redacted> remote-set-password" \
            env REMOTE_PASSWORD="$PASSWORD" remote-set-password \
            || die "remote-set-password failed"
    elif [ -f "$HOST_CONF" ]; then
        log "password already set ($HOST_CONF exists) — keeping it"
    else
        if [ "$ASSUME_YES" = 1 ] || [ "$DRY_RUN" = 1 ] || [ ! -t 0 ]; then
            [ "$DRY_RUN" = 1 ] && { echo "[dry-run] + remote-set-password (interactive prompt)" >&2; return 0; }
            die "no password provided and running non-interactively. Fix: set REMOTE_PASSWORD env var (or --password) and re-run."
        fi
        log "no password set yet — prompting (input hidden)"
        run remote-set-password || die "remote-set-password failed"
    fi
    # The host service runs as yourremote; a root-owned host.conf (written
    # by older installers) is unreadable to it — repair ownership.
    [ "$DRY_RUN" = 1 ] || fix_host_conf_ownership
}

fix_host_conf_ownership() {
    if [ "$(id -u)" = 0 ] && id yourremote >/dev/null 2>&1 \
        && [ -f "$HOST_CONF" ]; then
        chown yourremote:yourremote "$HOST_CONF" 2>/dev/null || true
        chmod 600 "$HOST_CONF" 2>/dev/null || true
    fi
}

# ---- step 5: service (systemd, or direct background start) ------------------------
setup_service() {
    if [ "$NO_SYSTEMD" = 1 ]; then
        warn "--no-systemd: skipping service enable/start."
        echo "Start the host manually with:  remote-host   (as your desktop user, inside the graphical session)"
        return 0
    fi
    if [ ! -d /run/systemd/system ]; then
        warn "systemd is not running as PID 1 (container?) — starting remote-host directly in the background."
        warn "It will NOT auto-start on boot; add it to the container's startup."
        start_host_nosystemd
        return 0
    fi
    log "enabling + starting remote-host"
    run systemctl enable --now remote-host \
        || die "systemctl enable --now failed. Fix: systemctl status remote-host; journalctl -u remote-host -n 50"
    if [ "$DRY_RUN" = 1 ]; then return 0; fi
    if systemctl is-active --quiet remote-host; then
        log "remote-host is active"
    else
        echo "--- journal tail ---" >&2
        journalctl -u remote-host -n 50 --no-pager >&2 || true
        die "remote-host is not active after start. Fix: inspect the journal above; check /etc/remote/host.conf exists."
    fi
}

start_host_nosystemd() {
    # remote-host listens on TCP 47800 (>1024): runs as the yourremote user.
    # sd_notify is a no-op without NOTIFY_SOCKET, so a plain background
    # start is safe.
    if pgrep -f "[r]emote-host" >/dev/null 2>&1; then
        log "remote-host already running — leaving it alone"
        return 0
    fi
    have runuser || die "need 'runuser' to start remote-host without systemd"
    [ "$DRY_RUN" = 1 ] && { echo "[dry-run] + start remote-host directly (no systemd)" >&2; return 0; }
    # Screen capture / input injection need the desktop session's DISPLAY
    # and X cookie. As root we copy the cookie to a yourremote-owned file.
    local xenv="" sess=""
    if sess="$(detect_desktop_session)"; then
        local xuser="${sess%%|*}" rest="${sess#*|}"
        local display="${rest%%|*}" xauth="${rest#*|}"
        log "desktop session: user=$xuser display=$display"
        xenv="DISPLAY=$display"
        if [ -n "$xauth" ]; then
            cp -f "$xauth" /etc/remote/xauthority \
                || die "could not copy X authority cookie from $xauth"
            chown yourremote:yourremote /etc/remote/xauthority
            chmod 600 /etc/remote/xauthority
            xenv="$xenv XAUTHORITY=/etc/remote/xauthority"
        else
            log "no XAUTHORITY cookie found — trying without one"
        fi
    else
        warn "no desktop X session detected — remote-host needs a display for screen capture."
    fi
    log "starting remote-host in the background as user yourremote"
    if [ -n "$xenv" ]; then
        # shellcheck disable=SC2086
        nohup env $xenv runuser -u yourremote -- /usr/bin/remote-host \
            >>/var/log/remote/host-stdout.log 2>&1 &
    else
        nohup runuser -u yourremote -- /usr/bin/remote-host \
            >>/var/log/remote/host-stdout.log 2>&1 &
    fi
    sleep 3
    if pgrep -f "[r]emote-host" >/dev/null 2>&1; then
        log "remote-host running (pid $(pgrep -f '[r]emote-host' | head -n1))"
    else
        die "remote-host did not stay up. Fix: see /var/log/remote/host-stdout.log and /var/log/remote/"
    fi
}

# Prints "user|display|xauthority" for the graphical X session, or nothing.
# Matches Xorg, Xwayland, and the container-typical Xvfb / Xvnc servers.
detect_desktop_session() {
    local xpid xuser xauth display sock name
    xpid=""
    for name in Xorg Xwayland Xvfb Xvnc Xvnc4 Xtigervnc X; do
        xpid="$(pgrep -o -x "$name" 2>/dev/null || true)"
        [ -n "$xpid" ] && break
    done
    [ -n "$xpid" ] || return 1
    xuser="$(ps -o user= -p "$xpid" 2>/dev/null | tr -d '[:space:]')"
    [ -n "$xuser" ] || return 1
    display=""
    for sock in /tmp/.X11-unix/X*; do
        if [ -S "$sock" ]; then display=":${sock##*/X}"; break; fi
    done
    [ -n "$display" ] || display=":0"
    # XAUTHORITY is best-effort: some servers (Xvfb/xhost setups) need no
    # cookie. Only pass it when we actually find the file.
    xauth="$(tr '\0' '\n' < "/proc/$xpid/environ" 2>/dev/null | grep '^XAUTHORITY=' | cut -d= -f2-)"
    if [ -z "$xauth" ]; then
        xauth="$(getent passwd "$xuser" 2>/dev/null | cut -d: -f6)/.Xauthority"
    fi
    [ -f "$xauth" ] || xauth=""
    printf '%s|%s|%s' "$xuser" "$display" "$xauth"
}

# ---- step 6: summary ---------------------------------------------------------------
print_summary() {
    local ver ip svc
    ver="$(cat /opt/remote/version.txt 2>/dev/null || deb_installed_version)"
    ver="${ver:-unknown}"
    ip="$(tailscale ip -4 2>/dev/null | head -n1 || true)"
    if [ "$NO_SYSTEMD" = 1 ]; then svc="(skipped: --no-systemd)";
    elif [ ! -d /run/systemd/system ] && pgrep -f "[r]emote-host" >/dev/null 2>&1; then svc="active (direct, no systemd)";
    elif systemctl is-active --quiet remote-host 2>/dev/null; then svc="active";
    else svc="NOT ACTIVE"; fi
    echo
    echo "================ Remote host ready ================"
    echo "  version:      $ver"
    echo "  tailscale ip: ${ip:-<none — tailscale not up>}"
    echo "  service:      $svc  (remote-host)"
    echo "  port:         $PORT (tailnet only)"
    echo
    echo "Next steps:"
    echo "  1. Pair your phone:  sudo yourremote-pair-code"
    echo "     (or scan the QR it prints), then enter the code in the app."
    echo "  2. In the Android app add this host: ${ip:-<tailscale-ip>}:$PORT"
    echo "     with the password you just set."
    [ -n "$ip" ] || echo "  !! tailscale has no IP — run: sudo yourremote-network-setup"
    echo "==================================================="
}

# ---- uninstall ------------------------------------------------------------------
do_uninstall() {
    log "removing the remote package"
    run apt-get purge -y remote || die "apt-get purge failed"
    echo "Removed. Config/logs may remain; to wipe fully:"
    echo "  sudo rm -rf /etc/remote /var/log/remote"
}

# ---- main -----------------------------------------------------------------------
main() {
    if [ "$DO_UNINSTALL" = 1 ]; then
        if [ "$ASSUME_YES" = 0 ] && [ ! -t 0 ]; then
            die "refusing to uninstall non-interactively without --yes. Fix: re-run with --yes."
        fi
        ensure_root; check_distro; do_uninstall; return 0
    fi
    if [ "$ASSUME_YES" = 0 ] && [ ! -t 0 ]; then
        # Piped install (curl ... | bash): stdin is the pipe, so no prompt
        # could ever be answered. Act as --yes; every step stays idempotent
        # and the wizard still prints the Tailscale auth URL when needed
        # (tailscale up itself waits for the browser login).
        ASSUME_YES=1
        echo "NOTE: stdin is not a terminal (piped install); enabling --yes." >&2
    fi
    ensure_root
    check_distro
    log "Remote host installer (dry-run: $DRY_RUN)"
    local deb want="$WANT_VERSION"
    # die here (main shell): a die inside resolve_deb only exits its $(...)
    # subshell, which would leave us installing a garbage path.
    deb="$(resolve_deb)" \
        || die "failed to obtain the Remote .deb package."
    install_deb "$deb" "$want"
    setup_tailscale
    setup_v4l2loopback
    setup_password
    setup_service
    print_summary
}

main "$@"
