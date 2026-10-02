"""host/monitor.py -- AGENT_STATUS (0x66) data collection for Remote v4.

Renamed from agent_status.py (v3). The v3 keys returned by collect_status()
are byte-compatible (same names, same types); v4 adds:

  load_avg      [1m, 5m, 15m] from os.getloadavg()
  top_processes [{pid, name, cpu_pct, mem_mb}] top 8 by CPU (real /proc scan)
  temperatures  [{sensor, label, temp_c}] from /sys/class/hwmon + thermal_zone
                ([] when the hardware exposes nothing -- never faked)
  gpu           {name, util_pct, temp_c, mem_used_mb, mem_total_mb} from
                nvidia-smi, best-effort; None when absent (never faked)
  uptime_s      seconds since boot from /proc/uptime (None when unreadable)
  boot_ts       unix epoch of boot (None when uptime is unreadable)

Stdlib only, except best-effort external binaries (systemctl, nvidia-smi).
Every probe is wrapped in try/except with honest defaults, so a missing
/proc entry or binary never breaks the reply -- absence is reported as
absence, never invented.
"""
import os
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


# -- v4 extensions -------------------------------------------------------------


def load_avg():
    """[1m, 5m, 15m] load averages; [0.0, 0.0, 0.0] when unreadable."""
    try:
        return [round(float(x), 2) for x in os.getloadavg()]
    except OSError:
        return [0.0, 0.0, 0.0]


def _proc_snapshot():
    """pid -> (name, utime_jiffies, stime_jiffies, rss_mb)."""
    snap = {}
    try:
        page_kb = os.sysconf("SC_PAGE_SIZE") // 1024
    except (OSError, ValueError):
        page_kb = 4
    try:
        pids = os.listdir("/proc")
    except OSError:
        return snap
    for pid in pids:
        if not pid.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % pid) as f:
                st = f.read()
        except OSError:
            continue
        # comm sits between parens and may itself contain spaces/parens
        lparen, rparen = st.find("("), st.rfind(")")
        if lparen < 0 or rparen < 0 or rparen <= lparen:
            continue
        name = st[lparen + 1:rparen]
        fields = st[rparen + 2:].split()
        # after comm: state ppid pgrp session tty_nr tpgid flags minflt
        # cminflt majflt cmajflt utime(11) stime(12) cutime cstime ... rss(21)
        if len(fields) < 22:
            continue
        try:
            utime = int(fields[11])
            stime = int(fields[12])
            rss_mb = int(fields[21]) * page_kb // 1024
        except (ValueError, IndexError):
            continue
        snap[int(pid)] = (name, utime, stime, rss_mb)
    return snap


def top_processes(limit=8):
    """Top `limit` processes by CPU over a short sample.

    Returns [{pid, name, cpu_pct, mem_mb}]. cpu_pct is percent of total
    system capacity (all CPUs), measured between two snapshots; processes
    that vanish between snapshots are skipped.
    """
    snap0 = _proc_snapshot()
    _, total0 = _cpu_times()
    time.sleep(SAMPLE_INTERVAL)
    snap1 = _proc_snapshot()
    _, total1 = _cpu_times()
    total_d = total1 - total0
    rows = []
    if total_d > 0:
        for pid, (name, u1, s1, rss_mb) in snap1.items():
            prev = snap0.get(pid)
            if prev is None:
                continue
            d = (u1 - prev[1]) + (s1 - prev[2])
            if d < 0:
                continue
            rows.append({
                "pid": pid,
                "name": name,
                "cpu_pct": round(100.0 * d / total_d, 1),
                "mem_mb": rss_mb,
            })
    rows.sort(key=lambda r: r["cpu_pct"], reverse=True)
    return rows[: max(0, int(limit))]


def temperatures():
    """[{sensor, label, temp_c}] from hwmon + thermal zones.

    Empty list when the machine exposes no sensors -- never fabricated.
    """
    out = []
    hwmon = "/sys/class/hwmon"
    try:
        devices = sorted(os.listdir(hwmon)) if os.path.isdir(hwmon) else []
    except OSError:
        devices = []
    for dev in devices:
        base = os.path.join(hwmon, dev)
        sensor = _read(os.path.join(base, "name")).strip() or dev
        try:
            files = sorted(os.listdir(base))
        except OSError:
            continue
        for f in files:
            if not (f.startswith("temp") and f.endswith("_input")):
                continue
            label = _read(os.path.join(base, f[:-len("_input")] + "_label")).strip()
            try:
                temp_c = int(_read(os.path.join(base, f)).strip()) / 1000.0
            except (ValueError, TypeError):
                continue
            out.append({"sensor": sensor,
                        "label": label or f,
                        "temp_c": round(temp_c, 1)})
    thermal = "/sys/class/thermal"
    try:
        zones = sorted(os.listdir(thermal)) if os.path.isdir(thermal) else []
    except OSError:
        zones = []
    for z in zones:
        if not z.startswith("thermal_zone"):
            continue
        sensor = _read(os.path.join(thermal, z, "type")).strip() or z
        try:
            temp_c = int(_read(os.path.join(thermal, z, "temp")).strip()) / 1000.0
        except (ValueError, TypeError):
            continue
        out.append({"sensor": sensor, "label": z, "temp_c": round(temp_c, 1)})
    return out


def gpu_info():
    """First NVIDIA GPU via nvidia-smi, best-effort.

    Returns {name, util_pct, temp_c, mem_used_mb, mem_total_mb}, or None
    when nvidia-smi is absent/fails -- never fabricated.
    """
    if shutil.which("nvidia-smi") is None:
        return None
    try:
        r = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=name,utilization.gpu,temperature.gpu,"
             "memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    lines = [ln for ln in (r.stdout or "").splitlines() if ln.strip()]
    if not lines:
        return None
    parts = [p.strip() for p in lines[0].split(",")]
    if len(parts) < 5:
        return None
    try:
        return {
            "name": parts[0],
            "util_pct": int(parts[1]),
            "temp_c": int(parts[2]),
            "mem_used_mb": int(parts[3]),
            "mem_total_mb": int(parts[4]),
        }
    except ValueError:
        return None


def uptime_s():
    """Seconds since boot from /proc/uptime; None when unreadable."""
    try:
        return int(float(_read("/proc/uptime").split()[0]))
    except (ValueError, IndexError):
        return None


def collect_status(monitored_services):
    """Build the full AGENT_STATUS payload dict.

    v3 keys are unchanged (byte-compatible). v4 adds load_avg,
    top_processes, temperatures, gpu, uptime_s, boot_ts.
    """
    try:
        cpu = cpu_pct()
    except Exception:  # noqa: BLE001
        cpu = 0.0
    # per-process CPU piggybacks on the sampling windows around us
    try:
        snap0 = _proc_snapshot()
        _, total0 = _cpu_times()
    except Exception:  # noqa: BLE001
        snap0, total0 = {}, 0
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
    try:
        snap1 = _proc_snapshot()
        _, total1 = _cpu_times()
        total_d = total1 - total0
        rows = []
        if total_d > 0:
            for pid, (name, u1, s1, rss_mb) in snap1.items():
                prev = snap0.get(pid)
                if prev is None:
                    continue
                d = (u1 - prev[1]) + (s1 - prev[2])
                if d < 0:
                    continue
                rows.append({"pid": pid, "name": name,
                             "cpu_pct": round(100.0 * d / total_d, 1),
                             "mem_mb": rss_mb})
        rows.sort(key=lambda r: r["cpu_pct"], reverse=True)
        top = rows[:8]
    except Exception:  # noqa: BLE001
        top = []
    try:
        load = load_avg()
    except Exception:  # noqa: BLE001
        load = [0.0, 0.0, 0.0]
    try:
        temps = temperatures()
    except Exception:  # noqa: BLE001
        temps = []
    try:
        gpu = gpu_info()
    except Exception:  # noqa: BLE001
        gpu = None
    try:
        up = uptime_s()
    except Exception:  # noqa: BLE001
        up = None
    boot = (int(time.time()) - up) if isinstance(up, int) else None
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
        "load_avg": load,
        "top_processes": top,
        "temperatures": temps,
        "gpu": gpu,
        "uptime_s": up,
        "boot_ts": boot,
    }
