"""host/displays.py -- DISPLAYS_LIST (0x68) data for Remote v3.

Parses `xrandr --query` for connected outputs (name, current mode WxH,
primary flag). If xrandr is missing or fails, falls back to a single
synthetic display built from the capture size (used in --self-test).
"""
import re
import shutil
import subprocess

# "HDMI-1 connected primary 1920x1080+0+0 (normal ..." or
# "DP-2 connected 1920x1080+1920+0 (normal ..."
_XRANDR_LINE = re.compile(
    r"^(\S+)\s+connected\s+(primary\s+)?(?:(\d+)x(\d+)\+)?"
)


def _from_xrandr():
    if shutil.which("xrandr") is None:
        return None
    try:
        out = subprocess.run(["xrandr", "--query"], capture_output=True,
                             timeout=5, text=True)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    displays = []
    for line in out.stdout.splitlines():
        m = _XRANDR_LINE.match(line)
        if not m:
            continue
        name, primary, w, h = m.group(1), m.group(2), m.group(3), m.group(4)
        if not w or not h:
            continue  # connected but no active mode: skip
        displays.append({
            "id": name,
            "name": name,
            "w": int(w),
            "h": int(h),
            "primary": bool(primary and primary.strip()),
        })
    if not displays:
        return None
    active = next((d["id"] for d in displays if d["primary"]), displays[0]["id"])
    return {"displays": displays, "active": active}


def get_displays(fallback_size=None):
    """Return {"displays": [{id, name, w, h, primary}], "active": id}.

    fallback_size: (w, h) used when xrandr is unavailable.
    """
    info = _from_xrandr()
    if info is not None:
        return info
    if fallback_size:
        w, h = fallback_size
        return {
            "displays": [{
                "id": "display-0",
                "name": "display-0",
                "w": int(w),
                "h": int(h),
                "primary": True,
            }],
            "active": "display-0",
        }
    return {"displays": [], "active": None}
