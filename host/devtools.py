"""host/devtools.py -- best-effort developer tool detection for Remote v4.

detect_devtools() probes a fixed tool list with shutil.which + `<tool>
--version` (java uses -version, go uses `version`). Returns a dict mapping
each FOUND tool to its version string, or True when the binary exists but
the version probe fails. Missing tools are ABSENT from the dict -- never
reported as null, never faked.

Stdlib + best-effort binaries only; every probe has a 5s timeout. This
feeds the AGENT_STATUS extended block (wired by the integrator).
"""
import shutil
import subprocess

# tool -> version argv (captured from stdout, falling back to stderr)
TOOLS = {
    "code": ["--version"],
    "docker": ["--version"],
    "python3": ["--version"],
    "node": ["--version"],
    "npm": ["--version"],
    "java": ["-version"],
    "go": ["version"],
    "gh": ["--version"],
    "gradle": ["--version"],
    "git": ["--version"],
    "rustc": ["--version"],
    "kubectl": ["version", "--client", "--short"],
}

VERSION_TIMEOUT = 5
VERSION_LIMIT = 120


def _tool_version(prog, argv):
    """First non-empty output line of the version probe, or None."""
    try:
        r = subprocess.run([prog] + argv, capture_output=True, text=True,
                           errors="replace", timeout=VERSION_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    out = (r.stdout or "").strip()
    if not out:
        out = (r.stderr or "").strip()  # e.g. java -version writes to stderr
    line = next((ln.strip() for ln in out.splitlines() if ln.strip()), "")
    return line[:VERSION_LIMIT] or None


def detect_devtools(tools=None):
    """Return {tool: version|True} for every tool found on PATH.

    `tools` optionally overrides the probe map (same shape as TOOLS).
    """
    found = {}
    for tool, argv in (TOOLS if tools is None else tools).items():
        prog = shutil.which(tool)
        if prog is None:
            continue  # absent: not reported, never faked
        ver = _tool_version(prog, argv)
        found[tool] = ver if ver else True
    return found
