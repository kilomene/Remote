"""host/webcam.py -- real v4l2 webcam capture for Remote v4.

Protocol v4 media numbers (canonical):
  WEBCAM_LIST  0x74 (c->s {} -> s->c JSON {"cameras": [{"id","name"}]})
  WEBCAM_FRAME 0x75 (c->s JSON {"id": "<device id>"} -> s->c JPEG bytes)

Permission flags "audio"/"webcam" are enforced by the host integrator;
this module is the capture engine only.

Frames are real JPEGs straight off the device: FF D8 ... FF D9, captured
with ffmpeg's v4l2 input (preferred) or fswebcam (fallback). There is no
synthetic-frame path anywhere in this module -- when no device or no
capture binary exists, grab_frame raises WebcamError with an honest
message.
"""
import logging
import os
import re
import shutil
import subprocess

LOG = logging.getLogger("remote-host")

SYSFS = "/sys/class/video4linux"
JPEG_SOI = b"\xff\xd8"
JPEG_EOI = b"\xff\xd9"
CAPTURE_TIMEOUT = 15


class WebcamError(Exception):
    pass


def _valid_device_id(device_id):
    """Device ids are the sysfs leaf names ('video0'). Anything else is
    rejected outright -- never interpolate untrusted text into /dev/."""
    if not isinstance(device_id, str) or not re.fullmatch(r"video\d+", device_id):
        raise WebcamError("invalid device id: %r" % (device_id,))
    return device_id


def list_cameras():
    """Return real cameras [{id, name}] from sysfs. [] when none/absent."""
    cams = []
    try:
        entries = sorted(os.listdir(SYSFS))
    except OSError:
        return []
    for entry in entries:
        if not re.fullmatch(r"video\d+", entry):
            continue
        name_path = os.path.join(SYSFS, entry, "name")
        try:
            with open(name_path, "r") as fh:
                name = fh.read().strip()
        except OSError:
            name = entry
        cams.append({"id": entry, "name": name or entry})
    return cams


def _grab_ffmpeg(device):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
           "-f", "v4l2", "-i", device,
           "-frames:v", "1", "-q:v", "5", "-f", "mjpeg", "-"]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=CAPTURE_TIMEOUT)
    except FileNotFoundError:
        raise WebcamError("ffmpeg not found on PATH")
    except subprocess.TimeoutExpired:
        raise WebcamError("ffmpeg capture timed out on %s" % device)
    if proc.returncode != 0:
        raise WebcamError("ffmpeg failed on %s: %s"
                          % (device, proc.stderr.decode("utf-8", "replace")[:200]))
    return proc.stdout


def _grab_fswebcam(device):
    cmd = ["fswebcam", "-q", "-d", device, "--jpeg", "85", "-"]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=CAPTURE_TIMEOUT)
    except FileNotFoundError:
        raise WebcamError("fswebcam not found on PATH")
    except subprocess.TimeoutExpired:
        raise WebcamError("fswebcam capture timed out on %s" % device)
    if proc.returncode != 0:
        raise WebcamError("fswebcam failed on %s: %s"
                          % (device, proc.stderr.decode("utf-8", "replace")[:200]))
    return proc.stdout


def grab_frame(device_id):
    """Capture one real JPEG frame from /dev/<device_id>.

    device_id must be an id returned by list_cameras() ('video0'). Raises
    WebcamError when the device is absent, no capture binary exists, or
    the output is not a JPEG.
    """
    dev = _valid_device_id(device_id)
    path = "/dev/" + dev
    if not os.path.exists(path):
        raise WebcamError("no such device: %s" % path)

    errors = []
    data = None
    if shutil.which("ffmpeg"):
        try:
            data = _grab_ffmpeg(path)
        except WebcamError as exc:
            errors.append(str(exc))
    else:
        errors.append("ffmpeg not found on PATH")
    if data is None and shutil.which("fswebcam"):
        try:
            data = _grab_fswebcam(path)
        except WebcamError as exc:
            errors.append(str(exc))
    if data is None:
        raise WebcamError("no capture binary available (%s)" % "; ".join(errors))

    if not data.startswith(JPEG_SOI) or not data.rstrip().endswith(JPEG_EOI):
        raise WebcamError("capture output from %s is not a JPEG (%d bytes)"
                          % (path, len(data)))
    return data
