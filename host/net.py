#!/usr/bin/env python3
"""Honest Tailscale integration for the Remote host.

We integrate with and automate the real `tailscale` CLI; we never
reimplement WireGuard. Every method degrades gracefully when Tailscale is
absent: status() reports installed:false and latency helpers return None.

Protocol v4 reference: NET_STATUS=0x76 (c->s {} -> s->c JSON
{online, tailscale_ip, peer_latency_ms, direct}).
"""
import json
import logging
import re
import shutil
import subprocess

LOG = logging.getLogger("remote-net")

TAILSCALE_CMD = "tailscale"
PING_COUNT = 3
CMD_TIMEOUT = 15


class TailscaleIntegration:
    """Wrapper around the real tailscale CLI."""

    def __init__(self, cmd=TAILSCALE_CMD, timeout=CMD_TIMEOUT):
        self.cmd = cmd
        self.timeout = timeout

    # -- availability ---------------------------------------------------

    def is_installed(self):
        """True iff the tailscale binary is on PATH."""
        return shutil.which(self.cmd) is not None

    # -- status ----------------------------------------------------------

    def _run(self, *args):
        try:
            return subprocess.run(
                [self.cmd, *args],
                capture_output=True, text=True, timeout=self.timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            LOG.warning("tailscale %s failed: %s", args[0], exc)
            return None

    @staticmethod
    def parse_status(raw):
        """Parse `tailscale status --json` output into a real dict.

        Returns {installed, online, tailscale_ip, peers:[{name, ip, online}]}.
        Never raises on malformed input.
        """
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except (ValueError, TypeError):
            return {"installed": True, "online": False,
                    "tailscale_ip": None, "peers": []}
        if not isinstance(data, dict):
            return {"installed": True, "online": False,
                    "tailscale_ip": None, "peers": []}
        self_info = data.get("Self") or {}
        ips = self_info.get("TailscaleIPs") or []
        tailnet_ip = ips[0] if ips else None
        online = bool(self_info.get("Online", False) and tailnet_ip)
        peers = []
        peer_map = data.get("Peer") or {}
        if isinstance(peer_map, dict):
            for key, p in peer_map.items():
                if not isinstance(p, dict):
                    continue
                pips = p.get("TailscaleIPs") or []
                peers.append({
                    "name": p.get("HostName") or p.get("DNSName") or key,
                    "ip": pips[0] if pips else None,
                    "online": bool(p.get("Online", False)),
                })
        return {"installed": True, "online": online,
                "tailscale_ip": tailnet_ip, "peers": peers}

    def status(self):
        """Real status via `tailscale status --json`; graceful when absent."""
        if not self.is_installed():
            return {"installed": False, "online": False,
                    "tailscale_ip": None, "peers": []}
        proc = self._run("status", "--json")
        if proc is None or proc.returncode != 0:
            LOG.warning("tailscale status failed: %s",
                        proc.stderr.strip() if proc else "no output")
            return {"installed": True, "online": False,
                    "tailscale_ip": None, "peers": []}
        return self.parse_status(proc.stdout)

    # -- setup wizard -----------------------------------------------------

    @staticmethod
    def extract_auth_url(output):
        """Pull the login URL out of `tailscale up` output, if any."""
        m = re.search(r"https?://\S+", output or "")
        return m.group(0).rstrip(").,") if m else None

    def up(self, login_server=None):
        """Run `tailscale up` (optionally --login-server URL).

        Returns (ok: bool, auth_url_or_detail: str). ok=True means the
        daemon reports connected; when a login is still required, ok=False
        and the second element is the auth URL for the wizard to show.
        """
        if not self.is_installed():
            return False, "tailscale not installed"
        args = ["up"]
        if login_server:
            args += ["--login-server", login_server]
        proc = self._run(*args)
        if proc is None:
            return False, "failed to run tailscale up"
        out = (proc.stdout or "") + (proc.stderr or "")
        auth_url = self.extract_auth_url(out)
        if proc.returncode == 0 and not auth_url:
            # connected without further action
            return True, "tailscale up: connected"
        if auth_url:
            return False, auth_url
        return False, out.strip()[:500] or "tailscale up failed"

    # -- latency ----------------------------------------------------------

    @staticmethod
    def parse_ping(output):
        """Parse `tailscale ping` output -> (latency_ms: float|None,
        direct: bool|None).

        Real tailscale ping lines look like:
          pong from 100.x.x.x (name) via 1.2.3.4:41641 in 12ms
        "via DERP(...)" means relayed (direct=False); anything else with a
        latency is a direct path. No pong -> (None, None).
        """
        if not output:
            return None, None
        latencies = []
        direct = None
        for line in output.splitlines():
            m = re.search(r"in\s+(\d+(?:\.\d+)?)\s*ms", line)
            if m:
                latencies.append(float(m.group(1)))
            if "DERP" in line:
                direct = False
            elif re.search(r"\bvia\b", line):
                direct = True
        if not latencies:
            return None, None
        avg = sum(latencies) / len(latencies)
        return avg, (direct if direct is not None else True)

    def peer_latency(self, ip):
        """`tailscale ping --c 3 <ip>` -> median-ish average ms, or None."""
        if not self.is_installed():
            return None
        proc = self._run("ping", "--c", str(PING_COUNT), ip)
        if proc is None or proc.returncode != 0:
            return None
        latency, _direct = self.parse_ping((proc.stdout or "") +
                                           (proc.stderr or ""))
        return latency

    def peer_direct(self, ip):
        """True if the ping path to ip is direct (not DERP), else None/False."""
        if not self.is_installed():
            return None
        proc = self._run("ping", "--c", str(PING_COUNT), ip)
        if proc is None or proc.returncode != 0:
            return None
        _latency, direct = self.parse_ping((proc.stdout or "") +
                                           (proc.stderr or ""))
        return direct


def net_status_payload(peer_ip=None, tailscale=None):
    """The exact JSON body for the NET_STATUS (0x76) reply.

    {online, tailscale_ip, peer_latency_ms, direct}. When tailscale is
    absent, online=false and the rest are None — never raises.
    """
    ts = tailscale or TailscaleIntegration()
    st = ts.status()
    latency = ts.peer_latency(peer_ip) if peer_ip else None
    direct = ts.peer_direct(peer_ip) if peer_ip else None
    return {
        "online": bool(st.get("online")),
        "tailscale_ip": st.get("tailscale_ip"),
        "peer_latency_ms": latency,
        "direct": direct,
    }
