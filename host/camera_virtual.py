"""host/camera_virtual.py -- "Camera for Verification" sink for Remote v4.

Concept: the user's Android phone has the camera; this Linux host may not
have one. On explicit request (CAMERA_START, 0x87), the phone streams H264
camera frames and this module presents them to Linux apps as a real
virtual camera (/dev/videoN) via v4l2loopback.

This module is the sink only. It decodes nothing itself -- there is no
H264 decoder in the stdlib, and faking one would be a lie. Instead it
spawns a real ffmpeg pipeline:

    ffmpeg -f h264 -i pipe:0 -pix_fmt yuv420p -vf <filters> -r FPS \
        -f v4l2 /dev/videoN

and writes each CAMERA_FRAME payload (raw H264 Annex-B, one access unit)
to ffmpeg's stdin. <filters> rotates/mirrors per the phone's orientation
(transpose/hflip) and scales to the presented geometry.

Device selection: /dev/video0 is NEVER assumed. The manager auto-selects:
first it reuses an existing v4l2loopback device carrying our card label
("YourRemote"); otherwise it loads v4l2loopback with video_nr=-1 (kernel
picks a free number) and adopts the newly appeared node. If neither works,
start() raises CameraError with an actionable message -- it never fakes
a device.

Verification-only, consent-gated rules (enforced here and by the host
integrator via the "camera" permission flag, fail-closed):

  * Explicit lifecycle only. No threads are started at import, nothing
    auto-starts. start() is called only on a CAMERA_START message.
  * If ffmpeg is missing, start() raises CameraError honestly.
  * Inactivity timeout: no CAMERA_FRAME for 60 s -> auto-stop. stop()
    terminates ffmpeg, closes stdin, and releases the device immediately.
  * stop() also best-effort unloads v4l2loopback, but ONLY when this
    manager loaded it and the unload succeeds (never touches a module
    the user was already using).
  * Nothing here records anything; frames flow pipe-to-pipe into the
    kernel v4l2 device and are never buffered or persisted.

Every start/stop is logged via the standard "remote-host" logger; the
integrator wires those into the JSON-lines log.
"""
import glob
import logging
import os
import shutil
import subprocess
import time

LOG = logging.getLogger("remote-host")

# Preferred explicit device (legacy / test use). The default is auto-select
# (device=None): never blindly assume video0.
DEVICE = "/dev/video0"
CARD_LABEL = "YourRemote"
INACTIVITY_TIMEOUT = 60.0          # seconds without a frame -> auto-stop
FRAME_MAX_BYTES = 8 * 1024 * 1024  # hard cap per CAMERA_FRAME payload
ACTIONABLE_MSG = ("v4l2loopback not available: sudo apt install v4l2loopback-dkms"
                  " && sudo modprobe v4l2loopback")

VALID_FACINGS = ("front", "rear")
VALID_ROTATIONS = (0, 90, 180, 270)
_MIN_W, _MAX_W = 160, 3840
_MIN_H, _MAX_H = 120, 2160
_MIN_FPS, _MAX_FPS = 1, 60


class CameraError(Exception):
    pass


def _validate_params(width, height, fps, facing, rotation=0, mirror=False):
    if not (isinstance(width, int) and _MIN_W <= width <= _MAX_W):
        raise CameraError("invalid width: %r" % (width,))
    if not (isinstance(height, int) and _MIN_H <= height <= _MAX_H):
        raise CameraError("invalid height: %r" % (height,))
    if not (isinstance(fps, int) and _MIN_FPS <= fps <= _MAX_FPS):
        raise CameraError("invalid fps: %r" % (fps,))
    if facing not in VALID_FACINGS:
        raise CameraError("invalid facing: %r (want front|rear)" % (facing,))
    if rotation not in VALID_ROTATIONS:
        raise CameraError("invalid rotation: %r (want 0|90|180|270)" % (rotation,))
    if not isinstance(mirror, bool):
        raise CameraError("invalid mirror flag: %r (want bool)" % (mirror,))
    return width, height, fps, facing, rotation, mirror


def _list_video_nodes():
    """Sorted /dev/videoN device nodes that currently exist."""
    nodes = []
    for path in glob.glob("/dev/video[0-9]*"):
        base = os.path.basename(path)
        if base[5:].isdigit():
            nodes.append(path)
    nodes.sort(key=lambda p: int(os.path.basename(p)[5:]))
    return nodes


def _card_label(node):
    """v4l2 card label for a device node, or None.

    v4l2loopback exposes the card_label module parameter here, so our own
    devices are recognizable without touching them.
    """
    base = os.path.basename(node)
    name_path = "/sys/class/video4linux/%s/name" % base
    try:
        with open(name_path, "r") as f:
            return f.read().strip()
    except OSError:
        return None


def _find_our_device():
    """First existing v4l2loopback node carrying our card label, else None."""
    for node in _list_video_nodes():
        if _card_label(node) == CARD_LABEL:
            return node
    return None


def _modprobe_new_device():
    """Load v4l2loopback letting the kernel pick a free number; return the
    newly appeared node, or None. Never fabricates: the node must really
    appear in /dev."""
    before = set(_list_video_nodes())
    try:
        proc = subprocess.run(
            ["modprobe", "v4l2loopback", "devices=1", "video_nr=-1",
             "card_label=%s" % CARD_LABEL],
            capture_output=True, timeout=15)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    # give udev a beat, then find the newcomer
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        after = set(_list_video_nodes())
        new = sorted(after - before,
                     key=lambda p: int(os.path.basename(p)[5:]))
        if new:
            node = new[0]
            if _card_label(node) == CARD_LABEL:
                return node
            # a node appeared but isn't ours (race with something else):
            # keep waiting briefly for our labeled one
        time.sleep(0.2)
    # last resort: any of our labeled devices (maybe udev was slow)
    return _find_our_device()


def _filter_chain(rotation, mirror, out_w, out_h):
    """ffmpeg -vf chain: rotate/mirror per the phone, then scale to the
    presented geometry."""
    parts = []
    if rotation == 90:
        parts.append("transpose=1")
    elif rotation == 270:
        parts.append("transpose=2")
    elif rotation == 180:
        parts.append("transpose=1,transpose=1")
    if mirror:
        parts.append("hflip")
    parts.append("scale=%d:%d" % (out_w, out_h))
    return ",".join(parts)


def _looks_like_annexb(data):
    return (data[:4] == b"\x00\x00\x00\x01"
            or data[:3] == b"\x00\x00\x01")


class VirtualCameraManager:
    """Owns at most one live ffmpeg -> v4l2loopback pipeline."""

    def __init__(self, device=None, inactivity_timeout=INACTIVITY_TIMEOUT):
        # device=None -> auto-select (never assume /dev/video0).
        # An explicit path keeps the legacy fixed-device behavior.
        self._wanted_device = device
        self.device = device  # resolved node once started; None until then
        self.inactivity_timeout = inactivity_timeout
        self._proc = None          # the ffmpeg subprocess
        self._params = None        # dict(width,height,fps,facing,rotation,mirror)
        self._last_frame_ts = None
        self._last_error = None
        self._module_loaded_by_us = False

    # -- state ----------------------------------------------------------
    @property
    def active(self):
        return self._proc is not None

    def status_dict(self):
        """Payload for CAMERA_STATUS (0x8A): s->c JSON. width/height/fps
        describe the PRESENTED geometry (post rotation), i.e. what a
        Linux application opening the device actually sees."""
        status = {
            "active": self.active,
            "device": self.device or "",
            "width": self._params["out_w"] if self._params else 0,
            "height": self._params["out_h"] if self._params else 0,
            "fps": self._params["fps"] if self._params else 0,
        }
        if self._last_error:
            status["error"] = self._last_error
        return status

    # -- device selection -------------------------------------------------
    def _ensure_device(self):
        """Resolve and return a usable /dev/videoN, or raise CameraError."""
        if self._wanted_device:
            if os.path.exists(self._wanted_device):
                return self._wanted_device
            raise CameraError(ACTIONABLE_MSG)
        node = _find_our_device()
        if node:
            LOG.info("camera: reusing existing virtual camera %s", node)
            return node
        node = _modprobe_new_device()
        if node:
            self._module_loaded_by_us = True
            LOG.info("camera: created virtual camera %s via v4l2loopback",
                     node)
            return node
        raise CameraError(ACTIONABLE_MSG)

    # -- lifecycle ------------------------------------------------------
    def start(self, width, height, fps, facing, rotation=0, mirror=False):
        """Bring up the virtual camera. Returns the device path.

        width/height/fps describe the ENCODED stream geometry (what the
        phone sends); rotation/mirror describe how the phone held the
        camera. The presented device geometry accounts for rotation.

        Raises CameraError if the camera is already active, if parameters
        are invalid, if no virtual device can be provided, or if ffmpeg is
        missing. Never fakes a device.
        """
        if self.active:
            raise CameraError("camera already in use")
        width, height, fps, facing, rotation, mirror = _validate_params(
            width, height, fps, facing, rotation, mirror)
        try:
            device = self._ensure_device()
        except CameraError as exc:
            self._last_error = str(exc)
            raise
        if not shutil.which("ffmpeg"):
            msg = "ffmpeg not found on PATH; cannot decode the H264 camera stream"
            self._last_error = msg
            raise CameraError(msg)

        # Presented geometry: a 90/270-degree rotation swaps the axes.
        out_w, out_h = (height, width) if rotation in (90, 270) else (width, height)
        vf = _filter_chain(rotation, mirror, out_w, out_h)
        argv = ["ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "h264", "-i", "pipe:0",
                "-pix_fmt", "yuv420p",
                "-vf", vf,
                "-r", str(fps),
                "-f", "v4l2", device]
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
        except (OSError, FileNotFoundError) as exc:
            msg = "failed to launch ffmpeg: %s" % exc
            self._last_error = msg
            raise CameraError(msg)
        if proc.poll() is not None:
            msg = "ffmpeg exited immediately (exit %s)" % proc.poll()
            self._last_error = msg
            raise CameraError(msg)

        self.device = device
        self._proc = proc
        self._params = {"width": width, "height": height,
                        "fps": fps, "facing": facing,
                        "rotation": rotation, "mirror": mirror,
                        "out_w": out_w, "out_h": out_h}
        self._last_frame_ts = time.monotonic()
        self._last_error = None
        LOG.info("camera started: %s %dx%d@%dfps facing=%s rotation=%d mirror=%s",
                 device, out_w, out_h, fps, facing, rotation, mirror)
        return device

    def write_frame(self, data):
        """Feed one CAMERA_FRAME payload (raw H264 Annex-B, one access
        unit) to the ffmpeg sink. Resets the inactivity timer.

        Malformed input (empty, over the cap, or not Annex-B) is rejected
        with CameraError and never reaches the decoder."""
        if not self.active:
            raise CameraError("camera is not active")
        if self.check_inactivity():
            raise CameraError("camera auto-stopped: no frames for %ds"
                              % int(self.inactivity_timeout))
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise CameraError("empty frame payload")
        if len(data) > FRAME_MAX_BYTES:
            raise CameraError("frame exceeds %d-byte cap" % FRAME_MAX_BYTES)
        if not _looks_like_annexb(data):
            raise CameraError("frame is not Annex-B H264 (missing start code)")
        try:
            self._proc.stdin.write(data)
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            self.stop()
            raise CameraError("ffmpeg sink died while writing a frame: %s" % exc)
        if self._proc.poll() is not None:
            rc = self._proc.poll()
            self.stop()
            raise CameraError("ffmpeg exited while streaming (exit %s)" % rc)
        self._last_frame_ts = time.monotonic()

    def check_inactivity(self, now=None):
        """Auto-stop if no CAMERA_FRAME arrived within the timeout.

        Returns True when it stopped the camera. The integrator should
        call this on every CAMERA_FRAME before write_frame(), or run it on
        a periodic sweep; write_frame() calls it itself as well.
        """
        if not self.active:
            return False
        now = time.monotonic() if now is None else now
        if now - self._last_frame_ts > self.inactivity_timeout:
            LOG.warning("camera auto-stopped: %.0fs without a frame",
                        now - self._last_frame_ts)
            self.stop()
            self._last_error = "stopped: inactivity timeout"
            return True
        return False

    def stop(self):
        """Terminate ffmpeg, close stdin, release the device immediately.
        Best-effort unloads v4l2loopback when this manager loaded it.
        Idempotent: safe to call when nothing is active."""
        if not self.active:
            return
        proc = self._proc
        device = self.device
        loaded_by_us = self._module_loaded_by_us
        self._proc = None
        self._params = None
        self._last_frame_ts = None
        self._module_loaded_by_us = False
        try:
            if proc.stdin:
                try:
                    proc.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        except OSError:
            pass
        LOG.info("camera stopped: %s released", device)
        if loaded_by_us:
            try:
                r = subprocess.run(["modprobe", "-r", "v4l2loopback"],
                                   capture_output=True, timeout=15)
                if r.returncode == 0:
                    LOG.info("camera: unloaded v4l2loopback (was loaded by us)")
                    self.device = None
                else:
                    LOG.info("camera: kept v4l2loopback loaded "
                             "(modprobe -r refused; device node remains)")
            except (OSError, subprocess.TimeoutExpired) as exc:
                LOG.info("camera: could not unload v4l2loopback: %s", exc)
