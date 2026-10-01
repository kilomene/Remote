"""host/agent_status.py -- AGENT_STATUS (0x66) data collection for Remote v3.

Stdlib only: CPU from /proc/stat (two samples), memory from /proc/meminfo,
disk via shutil.disk_usage("/"), network rates from /proc/net/dev (two
samples), service states via `systemctl is-active`. Every probe is wrapped
in try/except with sane defaults, so a missing /proc entry or systemctl
never breaks the reply.
"""
import shutil
import subprocess
import time

SAMPLE_INTERVAL = 0.5


def _read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def _cpu_times():
    """Return (idle, total) jiffies from the aggregate cpu line."""
    for line in _read("/proc/stat").splitlines():
        if line.startswith("cpu "):
            parts = [int(x) for x in line.split()[1:8]]
            idle = parts[3] + parts[4]  # idle + iowait
            return idle, sum(parts)
    return 0, 0


def cpu_pct():
    idle0, total0 = _cpu_times()
    time.sleep(SAMPLE_INTERVAL)
    idle1, total1 = _cpu_times()
    idle_d, total_d = idle1 - idle0, total1 - total0
    if total_d <= 0:
        return 0.0
    return round(max(0.0, min(100.0, (1.0 - idle_d / total_d) * 100.0)), 1)


def mem_mb():
    """Return (total_mb, used_mb) from /proc/meminfo."""
    total = avail = 0
    for line in _read("/proc/meminfo").splitlines():
        if line.startswith("MemTotal:"):
            total = int(line.split()[1])
        elif line.startswith("MemAvailable:"):
            avail = int(line.split()[1])
    total_mb = total // 1024
    used_mb = (total - avail) // 1024 if total else 0
    return total_mb, max(0, used_mb)


def disk_gb():
    """Return (total_gb, used_gb) for the root filesystem."""
    try:
        du = shutil.disk_usage("/")
        return round(du.total / 1e9, 1), round((du.total - du.free) / 1e9, 1)
    except OSError:
        return 0.0, 0.0


def _net_bytes():
    """Return (rx_bytes, tx_bytes) summed over non-loopback interfaces."""
    rx = tx = 0
    lines = _read("/proc/net/dev").splitlines()
    for line in lines[2:]:  # skip the two header lines
        if ":" not in line:
            continue
        iface, rest = line.split(":", 1)
        if iface.strip() == "lo":
            continue
        parts = rest.split()
        if len(parts) >= 9:
            try:
                rx += int(parts[0])
                tx += int(parts[8])
            except ValueError:
                pass
    return rx, tx


def net_bps():
    """Return (rx_bps, tx_bps) measured over SAMPLE_INTERVAL."""
    rx0, tx0 = _net_bytes()
    t0 = time.monotonic()
    time.sleep(SAMPLE_INTERVAL)
    rx1, tx1 = _net_bytes()
    dt = max(1e-3, time.monotonic() - t0)
    return round((rx1 - rx0) * 8 / dt), round((tx1 - tx0) * 8 / dt)


def service_active(name):
    """True if `systemctl is-active <name>` succeeds."""
    try:
        r = subprocess.run(["systemctl", "is-active", name],
                           capture_output=True, timeout=5)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def collect_status(monitored_services):
    """Build the full AGENT_STATUS payload dict."""
    try:
        cpu = cpu_pct()
    except Exception:  # noqa: BLE001
        cpu = 0.0
    try:
        mem_total, mem_used = mem_mb()
    except Exception:  # noqa: BLE001
        mem_total, mem_used = 0, 0
    try:
        disk_total, disk_used = disk_gb()
    except Exception:  # noqa: BLE001
        disk_total, disk_used = 0.0, 0.0
    try:
        rx_bps, tx_bps = net_bps()
    except Exception:  # noqa: BLE001
        rx_bps, tx_bps = 0, 0
    services = []
    for name in monitored_services or []:
        try:
            active = service_active(str(name))
        except Exception:  # noqa: BLE001
            active = False
        services.append({"name": str(name), "active": active})
    return {
        "cpu_pct": cpu,
        "mem_total_mb": mem_total,
        "mem_used_mb": mem_used,
        "disk_total_gb": disk_total,
        "disk_used_gb": disk_used,
        "net_rx_bps": rx_bps,
        "net_tx_bps": tx_bps,
        "services": services,
        "ts": time.time(),
    }
