#!/usr/bin/env python3
"""yourremote-network-setup: first-run Tailscale wizard for the Remote host.

Real, step-by-step setup -- nothing is faked:

  1. check_tailscale_installed()  -- is `tailscale` on PATH?
  2. install_tailscale()          -- adds Tailscale's official apt repo and
                                     installs the package (needs root; the
                                     user is asked to confirm first)
  3. tailscale_up()               -- runs `tailscale up`, prints the login
                                     auth URL for the user to open
  4. verify_tailscale()           -- `tailscale status` must succeed and
                                     `tailscale ip -4` must yield an address

Every step checks real return codes; any failure aborts with a clear
message. Flags: --headscale <url> (self-hosted control plane),
--check-only (just report state, change nothing).

Installed as /usr/bin/yourremote-network-setup.
"""
import argparse
import os
import re
import shutil
import subprocess
import sys

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
def install_tailscale(codename=None):
    """Install tailscale via the official apt repository. Needs root.

    Returns True on success; raises WizardError with the failing step's
    output otherwise. Every apt/curl invocation's return code is checked.
    """
    need_root()
    if not codename:
        rc, out, _ = run(["lsb_release", "-cs"], timeout=15)
        if rc != 0 or not out.strip():
            raise WizardError("cannot determine Ubuntu codename "
                              "(lsb_release failed)")
        codename = out.strip()
    steps = [
        (["apt-get", "update"],
         "apt-get update (prereqs)"),
        (["apt-get", "install", "-y", "curl", "ca-certificates"],
         "install curl + ca-certificates"),
    ]
    # NOTE: curl -o with argv list; the keyring URL is fixed upstream.
    keyring = "/usr/share/keyrings/tailscale-archive-keyring.gpg"
    steps.append(
        (["curl", "-fsSL",
          "https://pkgs.tailscale.com/stable/ubuntu/%s.noarch.gpg" % codename,
          "-o", keyring],
         "download Tailscale signing key"))
    repo_line = ("deb [signed-by=%s] https://pkgs.tailscale.com/stable/ubuntu "
                 "%s main\n" % (keyring, codename))
    steps.append((["apt-get", "update"], "apt-get update (tailscale repo)"))
    steps.append((["apt-get", "install", "-y", "tailscale"],
                  "install tailscale package"))

    for argv, label in steps:
        if argv[0] == "apt-get" and "update" in argv and label.endswith("(tailscale repo)"):
            # write the sources list just before this update
            try:
                with open("/etc/apt/sources.list.d/tailscale.list", "w") as f:
                    f.write(repo_line)
            except OSError as e:
                raise WizardError("cannot write apt sources list: %s" % e)
        print("  $ %s" % " ".join(argv))
        rc, out, err = run(argv, timeout=600)
        if rc != 0:
            raise WizardError("FAILED [%s] (exit %d):\n%s"
                              % (label, rc, (err or out).strip()[-2000:]))
        print("  ok: %s" % label)
    if not check_tailscale_installed():
        raise WizardError("apt reported success but `tailscale` is still "
                          "not on PATH")
    return True


# --------------------------------------------------------------------------
# Step 3: tailscale up (prints the auth URL)
# --------------------------------------------------------------------------
def tailscale_up(headscale_url=None):
    """Run `tailscale up`; print the auth URL; return it (or None).

    Returns the URL string when one was emitted, else None (already logged
    in or key-based auth). Raises WizardError when `tailscale up` fails.
    """
    argv = ["tailscale", "up"]
    if headscale_url:
        argv += ["--login-server", headscale_url]
    print("  $ %s" % " ".join(argv))
    rc, out, err = run(argv, timeout=300)
    combined = (out or "") + "\n" + (err or "")
    if rc != 0:
        raise WizardError("`tailscale up` failed (exit %d):\n%s"
                          % (rc, combined.strip()[-2000:]))
    m = AUTH_URL_RE.search(combined)
    url = m.group(0) if m else None
    if url:
        print()
        print("  ====================================================")
        print("  To authenticate, open this URL in a browser:")
        print("  %s" % url)
        print("  ====================================================")
        print()
    else:
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
    ap.add_argument("--check-only", action="store_true",
                    help="report Tailscale state without changing anything")
    ap.add_argument("--yes", action="store_true",
                    help="skip confirmation prompts (still needs root)")
    args = ap.parse_args()

    print("== Remote network setup ==")
    installed = check_tailscale_installed()
    print("tailscale installed: %s%s"
          % ("yes" + (" (%s)" % tailscale_version() if installed else ""),
             "" if installed else "no"))
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
                    "Install Tailscale from the official apt repository?"):
                print("declined; aborting.")
                return 1
            print("step 2/4: installing tailscale...")
            install_tailscale()
        else:
            print("step 2/4: already installed, skipping.")

        print("step 3/4: tailscale up...")
        url = tailscale_up(headscale_url=args.headscale)
        if url and not args.yes:
            input("Press Enter after opening the URL and logging in...")

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
