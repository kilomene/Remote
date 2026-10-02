#!/usr/bin/env python3
"""Real H264 decode loopback for the camera-for-verification path.

What this proves (with the REAL ffmpeg binary, no mocks in the decode
path):
  1. Annex-B H264 access units shaped like the Android MediaCodec output
     (SPS/PPS-prefixed IDR followed by P-frames) pass
     VirtualCameraManager.write_frame() unchanged.
  2. The exact bytes the manager would pipe to
     `ffmpeg -f h264 -i pipe:0 ... -f v4l2` decode cleanly with the real
     ffmpeg H264 decoder -- i.e. the Android encoder contract documented
     in PROTOCOL.md is sufficient for the host sink.

The v4l2 device itself is NOT needed: the manager's ffmpeg child is
replaced with a byte recorder (same trick as proto_v4_camera.py), and the
recorded bytes are fed to a real `ffmpeg -f h264 -i pipe:0 -f null -`
decode. If v4l2loopback + a real device existed we would go further; they
don't in CI, so this is the honest boundary.

Exits 0 printing "CAMERA DECODE LOOPBACK PASSED", or "skip" when ffmpeg
is absent.
"""
import os
import shutil
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "common"))
sys.path.insert(0, os.path.join(REPO, "host"))

import camera_virtual  # noqa: E402


class _Sink:
    def __init__(self):
        self.buf = bytearray()
        self.closed = False

    def write(self, data):
        self.buf += data

    def flush(self):
        pass

    def close(self):
        self.closed = True


class _Proc:
    def __init__(self, argv, **kw):
        self.argv = argv
        self.stdin = _Sink()
        self._rc = None
        _Proc.last = self

    def poll(self):
        return self._rc

    def terminate(self):
        self._rc = -15

    def kill(self):
        self._rc = -9

    def wait(self, timeout=None):
        return self._rc


def sh(argv, inp=None, timeout=60):
    p = subprocess.run(argv, input=inp, capture_output=True, timeout=timeout)
    return p


def main():
    if not shutil.which("ffmpeg"):
        print("skip: no ffmpeg on PATH")
        return
    # 1. Generate 30 frames of 640x480@30 H264 Annex-B with ffmpeg's own
    #    encoder: IDR + P-frames, SPS/PPS in-band -- the same shape the
    #    Android MediaCodec encoder must produce per PROTOCOL.md.
    gen = sh(["ffmpeg", "-hide_banner", "-loglevel", "error",
              "-f", "lavfi", "-i", "testsrc=size=640x480:rate=30:duration=1",
              "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
              "-g", "15", "-pix_fmt", "yuv420p",
              "-f", "h264", "pipe:1"])
    assert gen.returncode == 0, "generator failed: %s" % gen.stderr.decode()[-500:]
    raw = gen.stdout
    assert raw[:4] == b"\x00\x00\x00\x01" or raw[:3] == b"\x00\x00\x01", \
        "generator did not emit Annex-B"
    print("  generated %d bytes of Annex-B H264 (testsrc 640x480@30, 1s)" % len(raw))

    # 2. Split into access units on AUD/NAL boundaries the way the Android
    #    side must: one access unit per CAMERA_FRAME. Split on 4-byte start
    #    codes that begin an IDR (nal type 5) or non-IDR slice (type 1);
    #    keep SPS(7)/PPS(8) attached to the following IDR.
    units = []
    cur = bytearray()
    cur_has_slice = False  # cur already contains a type-1/5 slice NAL
    i = 0
    n = len(raw)
    while i < n:
        if raw[i:i + 4] == b"\x00\x00\x00\x01":
            sc = 4
        elif raw[i:i + 3] == b"\x00\x00\x01":
            sc = 3
        else:
            cur.append(raw[i])
            i += 1
            continue
        nal_type = raw[i + sc] & 0x1F
        if nal_type in (1, 5) and cur_has_slice:
            # new slice NAL while the current unit already has one:
            # flush the finished access unit; SPS/PPS/SEI stay attached
            # to the slice that follows them.
            units.append(bytes(cur))
            cur = bytearray()
            cur_has_slice = False
        if nal_type in (1, 5):
            cur_has_slice = True
        cur += raw[i:i + sc]
        i += sc
    if cur:
        units.append(bytes(cur))
    # first unit must carry SPS/PPS (types 7/8) before the IDR
    first = units[0]
    assert b"\x00\x00\x00\x01\x67" in first or b"\x00\x00\x01\x67" in first, \
        "first access unit lacks SPS"
    assert b"\x00\x00\x00\x01\x65" in first or b"\x00\x00\x01\x65" in first, \
        "first access unit lacks IDR"
    print("  split into %d access units; first is SPS/PPS-prefixed IDR (%d bytes)"
          % (len(units), len(first)))

    # 3. Feed every unit through the REAL manager with only the ffmpeg
    #    child mocked (byte recorder). write_frame must accept them all.
    real_popen = subprocess.Popen
    real_which = shutil.which
    subprocess.Popen = _Proc
    shutil.which = lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else real_which(name)
    try:
        mgr = camera_virtual.VirtualCameraManager(device="/dev/video0")
        # pretend the explicit device exists
        real_exists = os.path.exists
        os.path.exists = lambda p: True if p == "/dev/video0" else real_exists(p)
        try:
            mgr.start(640, 480, 30, "rear")
            for u in units:
                mgr.write_frame(u)
        finally:
            os.path.exists = real_exists
            mgr.stop()
    finally:
        subprocess.Popen = real_popen
        shutil.which = real_which
    piped = bytes(_Proc.last.stdin.buf)
    assert piped == raw, "manager altered the byte stream (%d vs %d)" % (
        len(piped), len(raw))
    print("  manager piped %d access units (%d bytes) untouched" % (len(units), len(piped)))

    # 4. Decode the piped bytes with the REAL ffmpeg H264 decoder, the
    #    same decoder the host sink uses. Count decoded frames.
    dec = sh(["ffmpeg", "-hide_banner", "-loglevel", "error",
              "-f", "h264", "-i", "pipe:0",
              "-pix_fmt", "yuv420p", "-f", "null", "-"],
             inp=piped, timeout=60)
    assert dec.returncode == 0, "decode failed: %s" % dec.stderr.decode()[-500:]
    # re-run with stats to count frames
    dec2 = sh(["ffmpeg", "-hide_banner", "-v", "info",
               "-f", "h264", "-i", "pipe:0",
               "-pix_fmt", "yuv420p", "-f", "null", "-"],
              inp=piped, timeout=60)
    frames = dec2.stderr.decode().count("frame=")
    print("  real ffmpeg decoded the stream cleanly (exit 0)")
    print("CAMERA DECODE LOOPBACK PASSED")


if __name__ == "__main__":
    main()
