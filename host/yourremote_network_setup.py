#!/usr/bin/env python3
"""yourremote-network-setup: first-run Tailscale wizard for the Remote host.

Real, step-by-step setup -- nothing is faked:

  1. check_tailscale_installed()  -- is `tailscale` on PATH?
  2. install_tailscale()          -- official apt repo when Tailscale
                                     publishes one for this distro, else the
                                     official static binaries (needs root;
                                     the user is asked to confirm first)
  3. tailscale_up()               -- runs `tailscale up`, prints the login
                                     auth URL for the user to open
  4. verify_tailscale()           -- `tailscale status` must succeed and
                                     `tailscale ip -4` must yield an address

Every step checks real return codes; any failure aborts with a clear
message. Flags: --headscale <url> (self-hosted control plane),
--authkey <key> (headless `tailscale up --authkey`; also read from the
TS_AUTHKEY environment variable; the key is never printed or logged),
--check-only (just report state, change nothing).

Installed as /usr/bin/yourremote-network-setup.
"""
import argparse
import os
import platform
import re
import select
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

AUTH_URL_RE = re.compile(r"https://login\.tailscale\.com/[^\s'\"]+")


class WizardError(Exception):
    """A wizard step failed; message is safe to show the user."""


def run(argv, check=False, timeout=120):
    """Run argv list; return (rc, stdout, stderr). Never uses shell=True."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout)
    except FileNotFoundError:
        raise WizardError("command not found: %s" % argv[0])
    except subprocess.TimeoutExpired:
        raise WizardError("timed out: %s" % " ".join(argv))
    return proc.returncode, proc.stdout, proc.stderr


def need_root():
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        raise WizardError("this step needs root; re-run with sudo")


# --------------------------------------------------------------------------
# Step 1: presence check
# --------------------------------------------------------------------------
def check_tailscale_installed():
    """True if the `tailscale` CLI is on PATH."""
    return shutil.which("tailscale") is not None


def tailscale_version():
    if not check_tailscale_installed():
        return None
    rc, out, _ = run(["tailscale", "version"], timeout=15)
    if rc != 0:
        return None
    return out.strip().splitlines()[0] if out.strip() else "unknown"


# --------------------------------------------------------------------------
# Step 2: install from Tailscale's official apt repo
# --------------------------------------------------------------------------
def distro_repo_flavor(os_release="/etc/os-release"):
    """Return 'debian' or 'ubuntu' for the Tailscale apt repo path.

    Tailscale publishes separate apt trees per family; using the wrong one
    404s (seen live: a Debian 13 'trixie' host under the ubuntu tree).
    Ubuntu derivatives (mint, pop, raspbian, ...) use the ubuntu tree.
    """
    info = {}
    try:
        with open(os_release) as f:
            for line in f:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    info[k] = v.strip('"').strip("'")
    except OSError:
        pass
    ident = info.get("ID", "").lower()
    like = info.get("ID_LIKE", "").lower().split()
    if ident == "debian" or "debian" in like:
        return "debian"
    return "ubuntu"


def install_tailscale(codename=None):
    """Install tailscale, preferring the official apt repo.

    Tries the apt repository first (it gets automatic updates); if Tailscale
    publishes no repo for this distro (seen live: Debian 13 'trixie' 404s
    under both trees), falls back to the official static binaries.
    Raises WizardError only if both paths fail.
    """
    try:
        return install_tailscale_repo(codename=codename)
    except WizardError as e:
        print("  repo install unavailable: %s" % str(e).splitlines()[0])
        print("  falling back to Tailscale static binaries...")
    return install_tailscale_static()


def tailscale_keyring_url(flavor, codename):
    return ("https://pkgs.tailscale.com/stable/%s/%s.noarch.gpg"
            % (flavor, codename))


def install_tailscale_repo(codename=None):
    """Install tailscale via the official apt repository. Needs root.

    Returns True on success; raises WizardError with the failing step's
    output otherwise. Every apt/curl invocation's return code is checked.
    A broken sources list written by a failed attempt is removed again.
    """
    need_root()
    flavor = distro_repo_flavor()
    if not codename:
        rc, out, _ = run(["lsb_release", "-cs"], timeout=15)
        if rc != 0 or not out.strip():
            raise WizardError("cannot determine distro codename "
                              "(lsb_release failed)")
        codename = out.strip()
    key_url = tailscale_keyring_url(flavor, codename)
    # Cheap pre-check: if Tailscale publishes no repo for this distro
    # (HTTP 404), fail fast before writing any apt state.
    rc, _, _ = run(["curl", "-fsSIL", "-o", "/dev/null", key_url], timeout=30)
    if rc != 0:
        raise WizardError(
            "Tailscale publishes no apt repo for %s/%s (tried %s)"
            % (flavor, codename, key_url))
    steps = [
        (["apt-get", "update"],
         "apt-get update (prereqs)"),
        (["apt-get", "install", "-y", "curl", "ca-certificates"],
         "install curl + ca-certificates"),
    ]
    # NOTE: curl -o with argv list; the keyring URL is fixed upstream per
    # distro family (debian vs ubuntu trees).
    keyring = "/usr/share/keyrings/tailscale-archive-keyring.gpg"
    steps.append((["curl", "-fsSL", key_url, "-o", keyring],
                  "download Tailscale signing key"))
    repo_line = ("deb [signed-by=%s] https://pkgs.tailscale.com/stable/%s "
                 "%s main\n" % (keyring, flavor, codename))
    steps.append((["apt-get", "update"], "apt-get update (tailscale repo)"))
    steps.append((["apt-get", "install", "-y", "tailscale"],
                  "install tailscale package"))

    sources_list = "/etc/apt/sources.list.d/tailscale.list"
    wrote_sources = False
    try:
        for argv, label in steps:
            if argv[0] == "apt-get" and "update" in argv and label.endswith("(tailscale repo)"):
                # write the sources list just before this update
                try:
                    with open(sources_list, "w") as f:
                        f.write(repo_line)
                    wrote_sources = True
                except OSError as e:
                    raise WizardError("cannot write apt sources list: %s" % e)
            print("  $ %s" % " ".join(argv))
            rc, out, err = run(argv, timeout=600)
            if rc != 0:
                raise WizardError("FAILED [%s] (exit %d):\n%s"
                                  % (label, rc, (err or out).strip()[-2000:]))
            print("  ok: %s" % label)
    except WizardError:
        if wrote_sources:
            try:
                os.unlink(sources_list)
            except OSError:
                pass
        raise
    if not check_tailscale_installed():
        raise WizardError("apt reported success but `tailscale` is still "
                          "not on PATH")
    return True


# --------------------------------------------------------------------------
# Static-binary fallback (no apt repo for this distro)
# --------------------------------------------------------------------------
STATIC_ARCHES = {"x86_64": "amd64", "aarch64": "arm64", "armv7l": "arm",
                 "armv6l": "arm"}


def static_arch(machine=None):
    """Map platform.machine() to Tailscale's static-build arch, or None."""
    return STATIC_ARCHES.get((machine or platform.machine()).lower())


def static_tarball_pick(html, arch):
    """Pick the newest tailscale_<ver>_<arch>.tgz from a /stable/ listing."""
    found = {}
    for m in re.finditer(r"tailscale_([0-9][0-9.\-]*?)_%s\.tgz" % re.escape(arch),
                         html or ""):
        ver = m.group(1)
        key = tuple(int(p) if p.isdigit() else 0
                    for p in re.split(r"[.\-]", ver))
        found[key] = m.group(0)
    if not found:
        return None
    return found[max(found)]


def tailscaled_unit():
    """systemd unit for a static tailscaled install (mirrors upstream's)."""
    return """[Unit]
Description=Tailscale node agent
Documentation=https://tailscale.com/kb/
Wants=network-pre.target
After=network-pre.target NetworkManager.service systemd-resolved.service

[Service]
ExecStart=/usr/local/bin/tailscaled --state=/var/lib/tailscale/tailscaled.state --socket=/run/tailscale/tailscaled.sock --port=41641
ExecStopPost=/usr/local/bin/tailscaled --cleanup
Restart=on-failure
RuntimeDirectory=tailscale
RuntimeDirectoryMode=0755
StateDirectory=tailscale
StateDirectoryMode=0700
CacheDirectory=tailscale
CacheDirectoryMode=0750
Type=notify
NotifyAccess=all

[Install]
WantedBy=multi-user.target
"""


def have_systemd():
    """True when systemd runs as PID 1 (canonical /run/systemd/system check)."""
    return os.path.isdir("/run/systemd/system")


def tailscaled_responding():
    """True when a tailscaled answers on the default socket (even logged-out).

    `tailscale status` exits non-zero when not logged in, but only says
    "failed to connect to local tailscaled" when no daemon is listening.
    """
    rc, out, err = run(["tailscale", "status"], timeout=15)
    combined = (out or "") + (err or "")
    return "failed to connect to local tailscaled" not in combined


def start_tailscaled_nosystemd():
    """Start tailscaled as a plain background daemon (no systemd as PID 1).

    For containers / WSL-style hosts. The daemon will NOT restart on boot;
    that limitation is printed loudly.
    """
    for d in ("/var/lib/tailscale", "/run/tailscale", "/var/log/tailscale"):
        os.makedirs(d, exist_ok=True)
    if not tailscaled_responding():
        logf = "/var/log/tailscale/tailscaled.log"
        lf = open(logf, "ab")
        subprocess.Popen(
            ["/usr/local/bin/tailscaled",
             "--state=/var/lib/tailscale/tailscaled.state",
             "--socket=/run/tailscale/tailscaled.sock",
             "--port=41641"],
            stdout=lf, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True,
            close_fds=True)
        print("  $ (background) tailscaled --state=/var/lib/tailscale/"
              "tailscaled.state --socket=/run/tailscale/tailscaled.sock "
              "--port=41641")
        for _ in range(25):
            time.sleep(1)
            if tailscaled_responding():
                break
        else:
            raise WizardError("tailscaled did not come up; see %s" % logf)
    print("  ok: tailscaled running without systemd "
          "(it will NOT auto-start on boot — add it to the container's "
          "startup or re-run the wizard)")


def kill_stale_tailscaled():
    """Kill tailscaled processes that aren't serving the socket (best-effort).

    A same-named process can linger after a failed start (seen live: pid
    38349 with no socket) and block a fresh daemon. Never kills our own
    process; pgrep -x avoids substring self-matches.
    """
    rc, out, _ = run(["pgrep", "-x", "tailscaled"], timeout=15)
    if rc != 0 or not (out or "").strip():
        return
    me = os.getpid()
    pids = [p for p in out.split() if p.isdigit() and int(p) != me]
    for sig in (None, "-9"):
        if not pids:
            return
        for p in pids:
            run(["kill"] + ([sig] if sig else []) + [p], timeout=10)
        time.sleep(2)
        rc, out, _ = run(["pgrep", "-x", "tailscaled"], timeout=15)
        pids = [p for p in (out or "").split()
                if p.isdigit() and int(p) != me]


def ensure_tailscaled_running():
    """Make sure a tailscaled daemon is answering before `tailscale up`.

    "Binary installed" does not imply "daemon running": a stale/broken
    tailscaled can linger without a socket (seen live), or the daemon may
    never have been started (reboot on a non-systemd host).
    """
    if tailscaled_responding():
        return
    print("  tailscaled not responding — (re)starting the daemon...")
    kill_stale_tailscaled()
    if have_systemd():
        rc, out, err = run(["systemctl", "enable", "--now", "tailscaled"],
                           timeout=120)
        if rc != 0:
            raise WizardError("FAILED [systemctl enable --now tailscaled] "
                              "(exit %d):\n%s"
                              % (rc, (err or out).strip()[-2000:]))
        print("  ok: tailscaled enabled+started via systemd")
    else:
        start_tailscaled_nosystemd()
    if not tailscaled_responding():
        raise WizardError("tailscaled still not responding after (re)start")


def install_tailscale_static():
    """Install Tailscale from the official static binaries. Needs root.

    Used when the apt repo doesn't cover this distro. Downloads the newest
    tailscale_<ver>_<arch>.tgz from pkgs.tailscale.com, installs
    tailscale/tailscaled to /usr/local/bin, and enables a systemd unit.
    """
    need_root()
    arch = static_arch()
    if not arch:
        raise WizardError(
            "unsupported CPU for static Tailscale binaries: %s "
            "(need one of: %s)" % (platform.machine(),
                                   ", ".join(sorted(STATIC_ARCHES))))
    rc, html, err = run(["curl", "-fsSL", "https://pkgs.tailscale.com/stable/"],
                        timeout=60)
    if rc != 0:
        raise WizardError("could not list Tailscale builds: %s"
                          % (err or html or "curl failed"))
    tgz = static_tarball_pick(html, arch)
    if not tgz:
        raise WizardError("no static Tailscale build for %s at "
                          "https://pkgs.tailscale.com/stable/" % arch)
    url = "https://pkgs.tailscale.com/stable/" + tgz
    print("  static build: %s" % tgz)
    tmpd = tempfile.mkdtemp(prefix="tailscale-static-")
    try:
        print("  $ curl -fsSL %s -o ..." % url)
        rc, _, err = run(["curl", "-fsSL", url, "-o",
                          os.path.join(tmpd, tgz)], timeout=600)
        if rc != 0:
            raise WizardError("static download failed: %s" % (err or url))
        try:
            with tarfile.open(os.path.join(tmpd, tgz), "r:gz") as tf:
                tf.extractall(tmpd, filter="data")
        except (tarfile.TarError, OSError) as e:
            raise WizardError("static archive corrupt: %s" % e)
        top = [d for d in os.listdir(tmpd)
               if d.startswith("tailscale_") and
               os.path.isdir(os.path.join(tmpd, d))]
        if not top:
            raise WizardError("unexpected static archive layout")
        src = os.path.join(tmpd, top[0])
        for name in ("tailscale", "tailscaled"):
            srcbin = os.path.join(src, name)
            if not os.path.isfile(srcbin):
                raise WizardError("static archive missing %s" % name)
            shutil.copy2(srcbin, "/usr/local/bin/" + name)
            os.chmod("/usr/local/bin/" + name, 0o755)
        print("  ok: installed tailscale/tailscaled to /usr/local/bin")
        if have_systemd():
            unit_path = "/etc/systemd/system/tailscaled.service"
            try:
                with open(unit_path, "w") as f:
                    f.write(tailscaled_unit())
            except OSError as e:
                raise WizardError("cannot write systemd unit: %s" % e)
            for argv in (["systemctl", "daemon-reload"],
                         ["systemctl", "enable", "--now", "tailscaled"]):
                print("  $ %s" % " ".join(argv))
                rc, out, err = run(argv, timeout=120)
                if rc != 0:
                    raise WizardError("FAILED [%s] (exit %d):\n%s"
                                      % (" ".join(argv), rc,
                                         (err or out).strip()[-2000:]))
                print("  ok: %s" % " ".join(argv[1:]))
        else:
            # container / WSL-style host: no systemd to manage the daemon
            print("  no systemd as PID 1 — starting tailscaled directly")
            start_tailscaled_nosystemd()
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)
    # A broken repo list from a failed/manual repo attempt would poison
    # future apt runs; a static install makes it redundant anyway.
    repo_list = "/etc/apt/sources.list.d/tailscale.list"
    try:
        with open(repo_list) as f:
            if "pkgs.tailscale.com" in f.read():
                os.unlink(repo_list)
                print("  ok: removed redundant tailscale apt source")
    except OSError:
        pass
    if not check_tailscale_installed():
        raise WizardError("static install finished but `tailscale` is still "
                          "not on PATH")
    return True


# --------------------------------------------------------------------------
# Step 3: tailscale up (prints the auth URL)
# --------------------------------------------------------------------------
def tailscale_up(headscale_url=None, authkey=None, timeout=300):
    """Run `tailscale up`; print the auth URL; return it (or None).

    Returns the URL string when one was emitted, else None (already logged
    in or key-based auth). Raises WizardError when `tailscale up` fails.

    authkey enables fully headless operation (tailscale up --authkey=...);
    it is NEVER printed or logged.

    Output is STREAMED live, not captured-until-exit: `tailscale up`
    prints the login URL and then BLOCKS waiting for the browser auth, so
    buffering everything would hide the URL until the timeout kills it.
    The URL is bannered the moment it appears.
    """
    argv = ["tailscale", "up"]
    if headscale_url:
        argv += ["--login-server", headscale_url]
    if authkey:
        argv += ["--authkey", authkey]
    shown = ["<redacted>" if (authkey and a == authkey) else a for a in argv]
    print("  $ %s" % " ".join(shown), flush=True)
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    url = None
    out_lines = []

    def handle_line(line):
        nonlocal url
        line = line.rstrip("\n")
        out_lines.append(line)
        print("  " + line, flush=True)
        if url is None:
            m = AUTH_URL_RE.search(line)
            if m:
                url = m.group(0)
                print()
                print("  ====================================================")
                print("  To authenticate, open this URL in a browser:")
                print("  " + url)
                print("  ====================================================")
                print(flush=True)

    try:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                proc.kill()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
                raise WizardError(
                    "`tailscale up` timed out after %d seconds waiting for "
                    "authentication.%s" % (
                        timeout,
                        " The login URL was printed above — open it in a "
                        "browser, then re-run." if url
                        else ""))
            r, _, _ = select.select([proc.stdout], [], [],
                                    min(1.0, remaining))
            if proc.stdout in r:
                line = proc.stdout.readline()
                if line == "":
                    break  # EOF: process exited
                handle_line(line)
            if proc.poll() is not None:
                # exited (maybe with no output): drain anything buffered
                for line in proc.stdout.read().splitlines():
                    handle_line(line)
                break
        # reap the child so returncode is set (breaking on EOF can happen
        # before poll() ever observed the exit)
        rc = proc.wait()
    finally:
        # never leave a stray `tailscale up` blocking behind us
        if proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
    if rc != 0:
        raise WizardError("`tailscale up` failed (exit %d):\n%s"
                          % (rc, "\n".join(out_lines)[-2000:]))
    if not url:
        print("  tailscale up succeeded with no auth URL (already logged in "
              "or using auth keys).")
    return url


# --------------------------------------------------------------------------
# Step 4: verify
# --------------------------------------------------------------------------
def verify_tailscale():
    """Verify the tailnet is up. Returns the IPv4 tailnet address.

    Raises WizardError unless `tailscale status` succeeds AND
    `tailscale ip -4` yields an address.
    """
    rc, out, err = run(["tailscale", "status"], timeout=30)
    if rc != 0:
        raise WizardError("`tailscale status` failed (exit %d):\n%s"
                          % (rc, (err or out).strip()[-1000:]))
    rc, out, err = run(["tailscale", "ip", "-4"], timeout=15)
    if rc != 0:
        raise WizardError("`tailscale ip -4` failed (exit %d)" % rc)
    ip = out.strip().splitlines()[0].strip() if out.strip() else ""
    if not ip:
        raise WizardError("tailscale is up but reported no IPv4 address")
    return ip


# --------------------------------------------------------------------------
# Wizard driver
# --------------------------------------------------------------------------
def confirm(prompt):
    try:
        ans = input(prompt + " [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return ans in ("y", "yes")


def main():
    ap = argparse.ArgumentParser(
        description="first-run Tailscale setup wizard for the Remote host")
    ap.add_argument("--headscale", metavar="URL", default=None,
                    help="use a self-hosted Headscale control plane")
    ap.add_argument("--authkey", metavar="KEY",
                    default=os.environ.get("TS_AUTHKEY"),
                    help="headless Tailscale auth key (or TS_AUTHKEY env); "
                         "never printed or logged")
    ap.add_argument("--check-only", action="store_true",
                    help="report Tailscale state without changing anything")
    ap.add_argument("--yes", action="store_true",
                    help="skip confirmation prompts (still needs root)")
    args = ap.parse_args()

    print("== Remote network setup ==")
    installed = check_tailscale_installed()
    if installed:
        print("tailscale installed: yes (%s)" % tailscale_version())
    else:
        print("tailscale installed: no")
    if args.check_only:
        if installed:
            try:
                print("tailnet ip: %s" % verify_tailscale())
            except WizardError as e:
                print("state: %s" % e)
                return 1
        return 0

    try:
        if not installed:
            if not args.yes and not confirm(
                    "Install Tailscale (official apt repo, or static "
                    "binaries if Tailscale has no repo for this distro)?"):
                print("declined; aborting.")
                return 1
            print("step 2/4: installing tailscale...")
            install_tailscale()
        else:
            print("step 2/4: already installed, skipping.")

        # the binary being present doesn't mean the daemon answers
        # (stale process, never started, reboot without systemd)
        ensure_tailscaled_running()

        print("step 3/4: tailscale up...")
        url = tailscale_up(headscale_url=args.headscale, authkey=args.authkey)
        if url and not args.yes:
            try:
                input("Press Enter after opening the URL and logging in...")
            except EOFError:
                # piped stdin (e.g. curl ... | install.sh): no prompt can be
                # answered; `tailscale up` already waited for the browser
                # login itself, so just continue to verification.
                pass
            except KeyboardInterrupt:
                print()
                return 1

        print("step 4/4: verifying...")
        ip = verify_tailscale()
    except WizardError as e:
        print("ERROR: %s" % e, file=sys.stderr)
        return 1

    print()
    print("Tailscale is up. This host's tailnet IP: %s" % ip)
    print("Point the Remote viewer at %s:47800." % ip)
    return 0


if __name__ == "__main__":
    sys.exit(main())
