#!/usr/bin/env python3
"""v4 camera-for-verification conformance check for Remote.

Exits 0 and prints "V4-CAMERA CHECKS PASSED" on success.

Protocol constants are asserted from common/remote_proto.py. The v4l2
sink is MOCKED (parent-authorized): subprocess.Popen is replaced with a
recorder so the exact ffmpeg argv, frame routing into stdin, and stop()
termination are asserted without needing real hardware. start() without
a device and a failing modprobe must raise CameraError with the exact
actionable message. Inactivity auto-stop is exercised by injecting an
old timestamp. Skip-if-absent integration tests run a real start/stop
cycle only when a real v4l2loopback device AND ffmpeg exist; otherwise
they print "skip".

No fakes: nothing here synthesizes a "camera" or a "decoded frame" and
calls it real.
"""
import os
import shutil
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "common"))
sys.path.insert(0, os.path.join(REPO, "host"))

import remote_proto       # noqa: E402
import camera_virtual     # noqa: E402


def check(name, fn):
    fn()
    print("ok: %s" % name)


def t_protocol_constants():
    assert remote_proto.CAMERA_START == 0x87, hex(remote_proto.CAMERA_START)
    assert remote_proto.CAMERA_STOP == 0x88, hex(remote_proto.CAMERA_STOP)
    assert remote_proto.CAMERA_FRAME == 0x89, hex(remote_proto.CAMERA_FRAME)
    assert remote_proto.CAMERA_STATUS == 0x8A, hex(remote_proto.CAMERA_STATUS)
    assert "camera" in remote_proto.PERMISSION_FLAGS
    assert len(remote_proto.PERMISSION_FLAGS) == 12, remote_proto.PERMISSION_FLAGS
    # frame payload must fit the global cap
    assert camera_virtual.FRAME_MAX_BYTES <= remote_proto.MAX_PAYLOAD


class _FakeStdin:
    def __init__(self):
        self.written = []
        self.closed = False

    def write(self, data):
        self.written.append(bytes(data))

    def flush(self):
        pass

    def close(self):
        self.closed = True


class _FakePopen:
    """Recorder standing in for subprocess.Popen."""
    last = None

    def __init__(self, argv, stdin=None, stdout=None, stderr=None):
        _FakePopen.last = self
        self.argv = argv
        self.kwargs = {"stdin": stdin, "stdout": stdout, "stderr": stderr}
        self.stdin = _FakeStdin()
        self.terminated = False
        self.killed = False
        self._rc = None

    def poll(self):
        return self._rc

    def terminate(self):
        self.terminated = True
        self._rc = -15

    def kill(self):
        self.killed = True
        self._rc = -9

    def wait(self, timeout=None):
        return self._rc


class _Patches:
    """Monkeypatch subprocess/os/shutil for one test."""

    def __init__(self, device_exists=True, ffmpeg=True, modprobe_rc=0):
        self.device_exists = device_exists
        self.ffmpeg = ffmpeg
        self.modprobe_rc = modprobe_rc

    def __enter__(self):
        self._popen = subprocess.Popen
        self._run = subprocess.run
        self._exists = os.path.exists
        self._which = shutil.which
        subprocess.Popen = _FakePopen

        rc = self.modprobe_rc

        def fake_run(argv, **kw):
            assert argv[0] == "modprobe", argv

            class R:
                returncode = rc
            return R()
        subprocess.run = fake_run

        exists = self.device_exists

        def fake_exists(path):
            if path == camera_virtual.DEVICE:
                return exists
            return self._exists(path)
        os.path.exists = fake_exists

        ffmpeg = self.ffmpeg

        def fake_which(name):
            if name == "ffmpeg":
                return "/usr/bin/ffmpeg" if ffmpeg else None
            return self._which(name)
        shutil.which = fake_which
        return self

    def __exit__(self, *exc):
        subprocess.Popen = self._popen
        subprocess.run = self._run
        os.path.exists = self._exists
        shutil.which = self._which
        return False


def t_start_builds_exact_ffmpeg_argv():
    with _Patches(device_exists=True):
        mgr = camera_virtual.VirtualCameraManager()
        dev = mgr.start(640, 480, 30, "rear")
        try:
            assert dev == "/dev/video0", dev
            assert mgr.active
            argv = _FakePopen.last.argv
            assert argv == ["ffmpeg", "-hide_banner", "-loglevel", "error",
                            "-f", "h264", "-i", "pipe:0",
                            "-pix_fmt", "yuv420p",
                            "-s", "640x480", "-r", "30",
                            "-f", "v4l2", "/dev/video0"], argv
            assert _FakePopen.last.kwargs["stdin"] == subprocess.PIPE
        finally:
            mgr.stop()


def t_write_frame_routes_bytes_to_stdin():
    with _Patches(device_exists=True):
        mgr = camera_virtual.VirtualCameraManager()
        mgr.start(320, 240, 15, "front")
        try:
            payload = b"\x00\x00\x00\x01\x65" + b"\xab" * 100
            mgr.write_frame(payload)
            got = b"".join(_FakePopen.last.stdin.written)
            assert got == payload, "frame bytes must reach ffmpeg stdin untouched"
        finally:
            mgr.stop()


def t_stop_terminates_process_and_closes_stdin():
    with _Patches(device_exists=True):
        mgr = camera_virtual.VirtualCameraManager()
        mgr.start(640, 480, 30, "rear")
        proc = _FakePopen.last
        mgr.stop()
        assert not mgr.active
        assert proc.terminated, "stop() must terminate ffmpeg"
        assert proc.stdin.closed, "stop() must close ffmpeg stdin"


def t_double_start_raises_already_in_use():
    with _Patches(device_exists=True):
        mgr = camera_virtual.VirtualCameraManager()
        mgr.start(640, 480, 30, "rear")
        try:
            try:
                mgr.start(640, 480, 30, "rear")
            except camera_virtual.CameraError as exc:
                assert "already in use" in str(exc), exc
                return
            raise AssertionError("second start() did not raise")
        finally:
            mgr.stop()


def t_inactivity_timeout_triggers_auto_stop():
    with _Patches(device_exists=True):
        mgr = camera_virtual.VirtualCameraManager(inactivity_timeout=60.0)
        mgr.start(640, 480, 30, "rear")
        # Inject an old timestamp: no frames for 61 s.
        mgr._last_frame_ts = time.monotonic() - 61.0
        assert mgr.check_inactivity() is True, "inactivity must auto-stop"
        assert not mgr.active
        proc = _FakePopen.last
        assert proc.terminated, "auto-stop must terminate ffmpeg"


def t_inactivity_reset_on_frame():
    with _Patches(device_exists=True):
        mgr = camera_virtual.VirtualCameraManager(inactivity_timeout=60.0)
        mgr.start(640, 480, 30, "rear")
        try:
            mgr._last_frame_ts = time.monotonic() - 59.0
            mgr.write_frame(b"\x00\x00\x00\x01\x41")
            assert mgr.active, "recent frame must reset the timer"
            assert mgr.check_inactivity() is False
        finally:
            mgr.stop()


def t_start_without_device_and_failed_modprobe_raises_actionable():
    # No /dev/video0 and modprobe fails -> exact actionable message.
    with _Patches(device_exists=False, modprobe_rc=1):
        mgr = camera_virtual.VirtualCameraManager()
        try:
            mgr.start(640, 480, 30, "rear")
        except camera_virtual.CameraError as exc:
            assert str(exc) == camera_virtual.ACTIONABLE_MSG, str(exc)
            assert "v4l2loopback-dkms" in str(exc)
            return
        raise AssertionError("start() without device did not raise")


def t_start_without_ffmpeg_raises_honestly():
    with _Patches(device_exists=True, ffmpeg=False):
        mgr = camera_virtual.VirtualCameraManager()
        try:
            mgr.start(640, 480, 30, "rear")
        except camera_virtual.CameraError as exc:
            assert "ffmpeg" in str(exc).lower(), exc
            return
        raise AssertionError("start() without ffmpeg did not raise")


def t_start_rejects_bad_params():
    with _Patches(device_exists=True):
        mgr = camera_virtual.VirtualCameraManager()
        for bad in [(0, 480, 30, "rear"), (640, 480, 0, "rear"),
                    (640, 480, 30, "left"), (99999, 480, 30, "rear")]:
            try:
                mgr.start(*bad)
            except camera_virtual.CameraError:
                continue
            raise AssertionError("bad params accepted: %r" % (bad,))
        assert not mgr.active


def t_status_dict_shape():
    with _Patches(device_exists=True):
        mgr = camera_virtual.VirtualCameraManager()
        mgr.start(1280, 720, 25, "front")
        try:
            st = mgr.status_dict()
            assert st["active"] is True
            assert st["device"] == "/dev/video0"
            assert st["width"] == 1280 and st["height"] == 720
            assert st["fps"] == 25
            assert "error" not in st
        finally:
            mgr.stop()
        st = mgr.status_dict()
        assert st["active"] is False
        assert st["width"] == 0


def t_integration_real_start_stop():
    # Skip-if-absent: only when a REAL v4l2loopback device and ffmpeg exist.
    if not os.path.exists("/dev/video0") or not shutil.which("ffmpeg"):
        print("skip: integration (no real /dev/video0 + ffmpeg)")
        return
    mgr = camera_virtual.VirtualCameraManager()
    dev = mgr.start(640, 480, 30, "rear")
    assert dev == "/dev/video0"
    assert mgr.active
    # Feed no frames; a real start/stop cycle is the check here.
    mgr.stop()
    assert not mgr.active
    print("ok: integration real start/stop (real /dev/video0)")


def main():
    check("protocol constants + camera permission flag", t_protocol_constants)
    check("start() builds the exact ffmpeg argv", t_start_builds_exact_ffmpeg_argv)
    check("write_frame routes bytes to ffmpeg stdin", t_write_frame_routes_bytes_to_stdin)
    check("stop() terminates ffmpeg and closes stdin", t_stop_terminates_process_and_closes_stdin)
    check("double start raises already-in-use", t_double_start_raises_already_in_use)
    check("inactivity timeout auto-stops", t_inactivity_timeout_triggers_auto_stop)
    check("frame resets inactivity timer", t_inactivity_reset_on_frame)
    check("no device + failed modprobe -> actionable CameraError",
          t_start_without_device_and_failed_modprobe_raises_actionable)
    check("missing ffmpeg raises honestly", t_start_without_ffmpeg_raises_honestly)
    check("start rejects bad params", t_start_rejects_bad_params)
    check("status_dict shape", t_status_dict_shape)
    check("integration real start/stop (skip-if-absent)", t_integration_real_start_stop)
    print("V4-CAMERA CHECKS PASSED")


if __name__ == "__main__":
    main()
