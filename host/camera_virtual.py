"""host/camera_virtual.py -- "Camera for Verification" sink for Remote v4.

Concept: the user's Android phone has the camera; this Linux host may not
have one. On explicit request (CAMERA_START, 0x87), the phone streams H264
camera frames and this module presents them to Linux apps as a real
virtual camera (/dev/video0) via v4l2loopback.

This module is the sink only. It decodes nothing itself -- there is no
H264 decoder in the stdlib, and faking one would be a lie. Instead it
spawns a real ffmpeg pipeline:

    ffmpeg -f h264 -i pipe:0 -pix_fmt yuv420p -s WxH -r FPS -f v4l2 /dev/video0

and writes each CAMERA_FRAME payload (raw H264 Annex-B, one access unit)
to ffmpeg's stdin.

Verification-only, consent-gated rules (enforced here and by the host
integrator via the "camera" permission flag, fail-closed):

  * Explicit lifecycle only. No threads are started at import, nothing
    auto-starts. start() is called only on a CAMERA_START message.
  * If /dev/video0 is absent the module attempts one best-effort
    `modprobe v4l2loopback ...`. If that fails, start() raises
    CameraError with an actionable message -- it never fakes a device.
  * If ffmpeg is missing, start() raises CameraError honestly.
  * Inactivity timeout: no CAMERA_FRAME for 60 s -> auto-stop. stop()
    terminates ffmpeg, closes stdin, and releases /dev/video0 immediately.
  * Nothing here records anything; frames flow pipe-to-pipe into the
    kernel v4l2 device and are never buffered or persisted.

Every start/stop is logged via the standard "remote-host" logger; the
integrator wires those into the JSON-lines log.
"""
import logging
import os
import shutil
import subprocess
import time

LOG = logging.getLogger("remote-host")

DEVICE = "/dev/video0"
INACTIVITY_TIMEOUT = 60.0          # seconds without a frame -> auto-stop
FRAME_MAX_BYTES = 8 * 1024 * 1024  # hard cap per CAMERA_FRAME payload
ACTIONABLE_MSG = ("v4l2loopback not available: sudo apt install v4l2loopback-dkms"
                  " && sudo modprobe v4l2loopback")

VALID_FACINGS = ("front", "rear")
_MIN_W, _MAX_W = 160, 3840
_MIN_H, _MAX_H = 120, 2160
_MIN_FPS, _MAX_FPS = 1, 60


class CameraError(Exception):
    pass


def _validate_params(width, height, fps, facing):
    if not (isinstance(width, int) and _MIN_W <= width <= _MAX_W):
        raise CameraError("invalid width: %r" % (width,))
    if not (isinstance(height, int) and _MIN_H <= height <= _MAX_H):
        raise CameraError("invalid height: %r" % (height,))
    if not (isinstance(fps, int) and _MIN_FPS <= fps <= _MAX_FPS):
        raise CameraError("invalid fps: %r" % (fps,))
    if facing not in VALID_FACINGS:
        raise CameraError("invalid facing: %r (want front|rear)" % (facing,))
    return width, height, fps, facing


def _ensure_device(device):
    """Return True if /dev/<device> exists; otherwise try modprobe once.

    Never fabricates a device: when both the device node and modprobe
    fail, the caller raises CameraError with ACTIONABLE_MSG.
    """
    if os.path.exists(device):
        return True
    try:
        proc = subprocess.run(
            ["modprobe", "v4l2loopback", "devices=1", "video_nr=0",
             "card_label=YourRemote"],
            capture_output=True, timeout=15)
    except FileNotFoundError:
        return False
    except subprocess.TimeoutExpired:
        return False
    if proc.returncode != 0:
        return False
    return os.path.exists(device)


class VirtualCameraManager:
    """Owns at most one live ffmpeg -> v4l2loopback pipeline."""

    def __init__(self, device=DEVICE, inactivity_timeout=INACTIVITY_TIMEOUT):
        self.device = device
        self.inactivity_timeout = inactivity_timeout
        self._proc = None          # the ffmpeg subprocess
        self._params = None        # dict(width,height,fps,facing) while active
        self._last_frame_ts = None
        self._last_error = None

    # -- state ----------------------------------------------------------
    @property
    def active(self):
        return self._proc is not None

    def status_dict(self):
        """Payload for CAMERA_STATUS (0x8A): s->c JSON."""
        status = {
            "active": self.active,
            "device": self.device,
            "width": self._params["width"] if self._params else 0,
            "height": self._params["height"] if self._params else 0,
            "fps": self._params["fps"] if self._params else 0,
        }
        if self._last_error:
            status["error"] = self._last_error
        return status

    # -- lifecycle ------------------------------------------------------
    def start(self, width, height, fps, facing):
        """Bring up the virtual camera. Returns the device path.

        Raises CameraError if the camera is already active, if parameters
        are invalid, if the v4l2loopback device cannot be provided, or if
        ffmpeg is missing. Never fakes a device.
        """
        if self.active:
            raise CameraError("camera already in use")
        width, height, fps, facing = _validate_params(width, height, fps, facing)
        if not _ensure_device(self.device):
            self._last_error = ACTIONABLE_MSG
            raise CameraError(ACTIONABLE_MSG)
        if not shutil.which("ffmpeg"):
            msg = "ffmpeg not found on PATH; cannot decode the H264 camera stream"
            self._last_error = msg
            raise CameraError(msg)

        argv = ["ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "h264", "-i", "pipe:0",
                "-pix_fmt", "yuv420p",
                "-s", "%dx%d" % (width, height),
                "-r", str(fps),
                "-f", "v4l2", self.device]
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

        self._proc = proc
        self._params = {"width": width, "height": height,
                        "fps": fps, "facing": facing}
        self._last_frame_ts = time.monotonic()
        self._last_error = None
        LOG.info("camera started: %s %dx%d@%dfps facing=%s",
                 self.device, width, height, fps, facing)
        return self.device

    def write_frame(self, data):
        """Feed one CAMERA_FRAME payload (raw H264 Annex-B, one access
        unit) to the ffmpeg sink. Resets the inactivity timer."""
        if not self.active:
            raise CameraError("camera is not active")
        if self.check_inactivity():
            raise CameraError("camera auto-stopped: no frames for %ds"
                              % int(self.inactivity_timeout))
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise CameraError("empty frame payload")
        if len(data) > FRAME_MAX_BYTES:
            raise CameraError("frame exceeds %d-byte cap" % FRAME_MAX_BYTES)
        try:
            self._proc.stdin.write(data)
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            self.stop()
            raise CameraError("ffmpeg sink died while writing a frame: %s" % exc)
        if self._proc.poll() is not None:
            self.stop()
            raise CameraError("ffmpeg exited while streaming (exit %s)"
                              % self._proc.poll())
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
        """Terminate ffmpeg, close stdin, release /dev/video0 immediately.
        Idempotent: safe to call when nothing is active."""
        if not self.active:
            return
        proc = self._proc
        self._proc = None
        self._params = None
        self._last_frame_ts = None
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
        LOG.info("camera stopped: %s released", self.device)
