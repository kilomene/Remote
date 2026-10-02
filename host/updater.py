#!/usr/bin/env python3
"""host/updater.py -- check for and install Remote .deb updates from GitHub.

Source of truth: https://github.com/kilomene/Remote/releases (the
`latest` release). Uses urllib with a 15s timeout; stdlib only.

Security note: there is no package-signature infrastructure beyond GitHub's
TLS. The release asset name is validated (must look like
remote_<version>_all.deb), the download size is checked against the asset
metadata GitHub reports, and dpkg itself validates the package structure.
Users who want stronger assurance should compare the asset's SHA with the
release page before applying.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request

RELEASES_LATEST_URL = "https://github.com/kilomene/Remote/releases/latest"
API_LATEST_URL = "https://api.github.com/repos/kilomene/Remote/releases/latest"
USER_AGENT = "remote-host-updater/1.0"
TIMEOUT = 15
ASSET_RE = re.compile(r"^remote_[^/]+_all\.deb$")


class UpdaterError(Exception):
    """Raised when an update check or install cannot proceed honestly."""


def installed_version(explicit=None):
    """Return the installed Remote version.

    Priority: explicit argument, /opt/remote/version.txt (shipped in the
    .deb), then `dpkg-query -W remote`. Raises UpdaterError if none works.
    """
    if explicit:
        return explicit.strip()
    for path in ("/opt/remote/version.txt",):
        try:
            with open(path) as f:
                v = f.read().strip()
            if v:
                return v
        except OSError:
            continue
    try:
        out = subprocess.run(
            ["dpkg-query", "-W", "-f=${Version}", "remote"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    raise UpdaterError(
        "cannot determine installed version (no /opt/remote/version.txt "
        "and dpkg-query failed); pass it explicitly"
    )


def _numeric_parts(version):
    """Split '1.2.3-rc1' -> ([1, 2, 3], 'rc1'). Non-numeric core -> ([], core)."""
    v = version.strip().lstrip("vV")
    core, _, suffix = v.partition("-")
    parts = []
    for p in core.split("."):
        if p.isdigit():
            parts.append(int(p))
        else:
            return [], v  # give up on numeric compare; compare as strings
    return parts, suffix


def compare_versions(a, b):
    """Semantic-ish compare. Returns -1 if a<b, 0 if equal, 1 if a>b."""
    pa, sa = _numeric_parts(a)
    pb, sb = _numeric_parts(b)
    if pa or pb:
        n = max(len(pa), len(pb))
        pa += [0] * (n - len(pa))
        pb += [0] * (n - len(pb))
        if pa != pb:
            return -1 if pa < pb else 1
        # same numbers: a release beats a pre-release suffix
        if sa == sb:
            return 0
        if not sa:
            return 1
        if not sb:
            return -1
        return -1 if sa < sb else (1 if sa > sb else 0)
    # no numeric core on either side: plain string compare
    return -1 if a < b else (1 if a > b else 0)


def parse_release_payload(payload):
    """Parse a GitHub releases/latest API payload (dict) into our shape.

    Pure function over already-decoded JSON -- this is what the tests feed
    with synthetic input. Raises UpdaterError on malformed payloads.
    """
    if not isinstance(payload, dict):
        raise UpdaterError("release payload is not a JSON object")
    tag = payload.get("tag_name")
    if not tag:
        raise UpdaterError("release payload has no tag_name")
    assets = []
    for a in payload.get("assets") or []:
        name = a.get("name", "")
        url = a.get("browser_download_url", "")
        if name and url:
            assets.append({"name": name, "url": url, "size": a.get("size")})
    return {
        "tag": tag,
        "version": tag.lstrip("vV"),
        "name": payload.get("name") or tag,
        "published_at": payload.get("published_at"),
        "assets": assets,
    }


def fetch_latest(timeout=TIMEOUT):
    """GET the latest release from the GitHub API. Returns parsed dict."""
    req = urllib.request.Request(
        API_LATEST_URL, headers={"User-Agent": USER_AGENT,
                                 "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise UpdaterError("no releases published yet for kilomene/Remote")
        raise UpdaterError("GitHub API HTTP %s: %s" % (e.code, e.reason))
    except urllib.error.URLError as e:
        raise UpdaterError("could not reach api.github.com: %s" % e.reason)
    except (json.JSONDecodeError, ValueError) as e:
        raise UpdaterError("GitHub API returned invalid JSON: %s" % e)
    return parse_release_payload(payload)


def check_update(current=None, timeout=TIMEOUT):
    """Return {current, latest, update_available, assets:[{name,url,size}]}."""
    cur = installed_version(current)
    rel = fetch_latest(timeout=timeout)
    return {
        "current": cur,
        "latest": rel["version"],
        "update_available": compare_versions(cur, rel["version"]) < 0,
        "release_name": rel["name"],
        "published_at": rel["published_at"],
        "assets": rel["assets"],
    }


def _download(url, dest, expected_size=None, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp, \
                open(dest, "wb") as f:
            shutil.copyfileobj(resp, f, length=1024 * 256)
    except urllib.error.URLError as e:
        raise UpdaterError("download failed: %s" % e.reason)
    if expected_size is not None:
        got = os.path.getsize(dest)
        if got != expected_size:
            raise UpdaterError(
                "download size mismatch: expected %d bytes, got %d"
                % (expected_size, got))


def download_and_install(asset_url, expected_size=None, asset_name=None):
    """Download a release .deb and install it with dpkg -i.

    Requires root (dpkg needs it). Raises UpdaterError otherwise or on any
    failure. Returns the dpkg output on success.
    """
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        raise UpdaterError("installing a .deb requires root; re-run with sudo")
    name = asset_name or asset_url.rsplit("/", 1)[-1].split("?", 1)[0]
    if not ASSET_RE.match(name):
        raise UpdaterError(
            "refusing to install %r: not a remote_*_all.deb release asset" % name)
    tmpdir = tempfile.mkdtemp(prefix="remote-update-")
    deb_path = os.path.join(tmpdir, name)
    try:
        _download(asset_url, deb_path, expected_size=expected_size)
        proc = subprocess.run(
            ["dpkg", "-i", deb_path],
            capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            raise UpdaterError("dpkg -i failed:\n%s" % (proc.stderr or proc.stdout))
        return proc.stdout
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
