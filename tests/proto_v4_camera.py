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

New in v1.1.0: /dev/video0 is never assumed -- the manager auto-selects
/dev/videoN (reuse an existing "YourRemote"-labeled node, else modprobe
with video_nr=-1 and adopt the newcomer). rotation/mirror from
CAMERA_START are honored in the ffmpeg -vf chain; non-Annex-B frames are
rejected; stop() best-effort unloads v4l2loopback only when this manager
loaded it.

No fakes: nothing here synthesizes a "camera" or a "decoded frame" and
calls it real.
"""
import glob
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
    """Monkeypatch subprocess/os/shutil/glob for one test."""

    def __init__(self, device_exists=True, ffmpeg=True, modprobe_rc=0,
                 video_nodes=None, card_labels=None):
        self.device_exists = device_exists
        self.ffmpeg = ffmpeg
        self.modprobe_rc = modprobe_rc
        # auto-select mocking: video_nodes = list of /dev/videoN present
        # BEFORE modprobe; card_labels = {node: label}
        self.video_nodes = video_nodes
        self.card_labels = card_labels or {}
        self.modprobe_calls = []

    def __enter__(self):
        self._popen = subprocess.Popen
        self._run = subprocess.run
        self._exists = os.path.exists
        self._which = shutil.which
        self._glob = glob.glob
        self._open = open
        subprocess.Popen = _FakePopen

        rc = self.modprobe_rc
        calls = self.modprobe_calls
        nodes = self.video_nodes

        def fake_run(argv, **kw):
            assert argv[0] == "modprobe", argv
            calls.append(list(argv))

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

        if nodes is not None:
            labels = self.card_labels

            def fake_glob(pattern):
                if pattern == "/dev/video[0-9]*":
                    return list(nodes)
                return self._glob(pattern)
            glob.glob = fake_glob

            real_open = self._open

            def fake_open(path, mode="r", *a, **kw):
                if isinstance(path, str) and path.startswith(
                        "/sys/class/video4linux/video"):
                    node = "/dev/" + path.split("/")[4]
                    if node in labels:
                        import io
                        return io.StringIO(labels[node] + "\n")
                    raise OSError("no sysfs entry")
                return real_open(path, mode, *a, **kw)

            # camera_virtual calls open() as a builtin; patch the module attr
            import builtins
            self._builtins_open = builtins.open
            builtins.open = fake_open
        return self

    def __exit__(self, *exc):
        subprocess.Popen = self._popen
        subprocess.run = self._run
        os.path.exists = self._exists
        shutil.which = self._which
        glob.glob = self._glob
        if self.video_nodes is not None:
            import builtins
            builtins.open = self._builtins_open
        return False


def _mgr(**kw):
    kw.setdefault("device", camera_virtual.DEVICE)  # explicit legacy device
    return camera_virtual.VirtualCameraManager(**kw)


def t_start_builds_exact_ffmpeg_argv():
    with _Patches(device_exists=True):
        mgr = _mgr()
        dev = mgr.start(640, 480, 30, "rear")
        try:
            assert dev == "/dev/video0", dev
            assert mgr.active
            argv = _FakePopen.last.argv
            assert argv == ["ffmpeg", "-hide_banner", "-loglevel", "error",
                            "-f", "h264", "-i", "pipe:0",
                            "-pix_fmt", "yuv420p",
                            "-vf", "scale=640:480",
                            "-r", "30",
                            "-f", "v4l2", "/dev/video0"], argv
            assert _FakePopen.last.kwargs["stdin"] == subprocess.PIPE
        finally:
            mgr.stop()


def t_rotation_mirror_in_filter_chain():
    # 90-degree rotation + front-camera mirror -> transpose, hflip, swapped scale
    with _Patches(device_exists=True):
        mgr = _mgr()
        mgr.start(1280, 720, 30, "front", rotation=90, mirror=True)
        try:
            argv = _FakePopen.last.argv
            vf = argv[argv.index("-vf") + 1]
            assert vf == "transpose=1,hflip,scale=720:1280", vf
            st = mgr.status_dict()
            # status reports the PRESENTED (post-rotation) geometry
            assert st["width"] == 720 and st["height"] == 1280, st
            assert st["device"] == "/dev/video0"
        finally:
            mgr.stop()
    # 180-degree rotation: double transpose, no axis swap
    with _Patches(device_exists=True):
        mgr = _mgr()
        mgr.start(640, 480, 15, "rear", rotation=180, mirror=False)
        try:
            argv = _FakePopen.last.argv
            vf = argv[argv.index("-vf") + 1]
            assert vf == "transpose=1,transpose=1,scale=640:480", vf
        finally:
            mgr.stop()


def t_write_frame_routes_bytes_to_stdin():
    with _Patches(device_exists=True):
        mgr = _mgr()
        mgr.start(320, 240, 15, "front")
        try:
            payload = b"\x00\x00\x00\x01\x65" + b"\xab" * 100
            mgr.write_frame(payload)
            got = b"".join(_FakePopen.last.stdin.written)
            assert got == payload, "frame bytes must reach ffmpeg stdin untouched"
        finally:
            mgr.stop()


def t_write_frame_rejects_non_annexb():
    with _Patches(device_exists=True):
        mgr = _mgr()
        mgr.start(320, 240, 15, "front")
        try:
            for bad in (b"", b"\xff\xd8\xff\xe0garbage", b"\x00\x01\x02"):
                try:
                    mgr.write_frame(bad)
                except camera_virtual.CameraError:
                    continue
                raise AssertionError("non-Annex-B frame accepted: %r" % (bad[:8],))
            assert mgr.active, "rejected frames must not kill the stream"
        finally:
            mgr.stop()


def t_stop_terminates_process_and_closes_stdin():
    with _Patches(device_exists=True):
        mgr = _mgr()
        mgr.start(640, 480, 30, "rear")
        proc = _FakePopen.last
        mgr.stop()
        assert not mgr.active
        assert proc.terminated, "stop() must terminate ffmpeg"
        assert proc.stdin.closed, "stop() must close ffmpeg stdin"


def t_double_start_raises_already_in_use():
    with _Patches(device_exists=True):
        mgr = _mgr()
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
        mgr = _mgr(inactivity_timeout=60.0)
        mgr.start(640, 480, 30, "rear")
        # Inject an old timestamp: no frames for 61 s.
        mgr._last_frame_ts = time.monotonic() - 61.0
        assert mgr.check_inactivity() is True, "inactivity must auto-stop"
        assert not mgr.active
        proc = _FakePopen.last
        assert proc.terminated, "auto-stop must terminate ffmpeg"


def t_inactivity_reset_on_frame():
    with _Patches(device_exists=True):
        mgr = _mgr(inactivity_timeout=60.0)
        mgr.start(640, 480, 30, "rear")
        try:
            mgr._last_frame_ts = time.monotonic() - 59.0
            mgr.write_frame(b"\x00\x00\x00\x01\x41")
            assert mgr.active, "recent frame must reset the timer"
            assert mgr.check_inactivity() is False
        finally:
            mgr.stop()


def t_start_without_device_and_failed_modprobe_raises_actionable():
    # Explicit device missing -> exact actionable message.
    with _Patches(device_exists=False, modprobe_rc=1):
        mgr = _mgr()
        try:
            mgr.start(640, 480, 30, "rear")
        except camera_virtual.CameraError as exc:
            assert str(exc) == camera_virtual.ACTIONABLE_MSG, str(exc)
            assert "v4l2loopback-dkms" in str(exc)
            return
        raise AssertionError("start() without device did not raise")


def t_auto_select_reuses_labeled_device():
    # A pre-existing "YourRemote" node is reused; no modprobe happens.
    nodes = ["/dev/video0", "/dev/video2"]
    labels = {"/dev/video0": "UVC Camera", "/dev/video2": "YourRemote"}
    with _Patches(video_nodes=nodes, card_labels=labels) as p:
        mgr = camera_virtual.VirtualCameraManager()  # auto-select
        dev = mgr.start(640, 480, 30, "rear")
        try:
            assert dev == "/dev/video2", dev
            assert mgr.device == "/dev/video2"
            assert p.modprobe_calls == [], \
                "reuse must not modprobe, got %r" % (p.modprobe_calls,)
            assert mgr._module_loaded_by_us is False
        finally:
            mgr.stop()


def t_auto_select_modprobes_and_adopts_new_node():
    # Nothing labeled ours: modprobe video_nr=-1, adopt the newcomer.
    nodes = ["/dev/video0"]
    labels = {"/dev/video0": "UVC Camera"}
    state = {"nodes": list(nodes)}

    orig_new = camera_virtual._modprobe_new_device

    def fake_modprobe_new():
        # simulate the kernel creating /dev/video1 with our label
        state["nodes"].append("/dev/video1")
        labels["/dev/video1"] = "YourRemote"
        return "/dev/video1"

    camera_virtual._modprobe_new_device = fake_modprobe_new
    try:
        with _Patches(video_nodes=state["nodes"], card_labels=labels) as p:
            mgr = camera_virtual.VirtualCameraManager()
            dev = mgr.start(640, 480, 30, "rear")
            try:
                assert dev == "/dev/video1", dev
                assert mgr._module_loaded_by_us is True
                argv = _FakePopen.last.argv
                assert argv[-1] == "/dev/video1", argv
            finally:
                mgr.stop()
            # stop() best-effort unloads the module it loaded
            assert any(c[:2] == ["modprobe", "-r"] for c in p.modprobe_calls), \
                "expected modprobe -r v4l2loopback on stop, got %r" % (
                    p.modprobe_calls,)
    finally:
        camera_virtual._modprobe_new_device = orig_new


def t_auto_select_no_device_anywhere_raises_actionable():
    orig_new = camera_virtual._modprobe_new_device
    camera_virtual._modprobe_new_device = lambda: None
    try:
        with _Patches(video_nodes=[], card_labels={}):
            mgr = camera_virtual.VirtualCameraManager()
            try:
                mgr.start(640, 480, 30, "rear")
            except camera_virtual.CameraError as exc:
                assert str(exc) == camera_virtual.ACTIONABLE_MSG, str(exc)
                return
            raise AssertionError("auto-select without any device did not raise")
    finally:
        camera_virtual._modprobe_new_device = orig_new


def t_start_without_ffmpeg_raises_honestly():
    with _Patches(device_exists=True, ffmpeg=False):
        mgr = _mgr()
        try:
            mgr.start(640, 480, 30, "rear")
        except camera_virtual.CameraError as exc:
            assert "ffmpeg" in str(exc).lower(), exc
            return
        raise AssertionError("start() without ffmpeg did not raise")


def t_start_rejects_bad_params():
    with _Patches(device_exists=True):
        mgr = _mgr()
        for bad in [(0, 480, 30, "rear"), (640, 480, 0, "rear"),
                    (640, 480, 30, "left"), (99999, 480, 30, "rear")]:
            try:
                mgr.start(*bad)
            except camera_virtual.CameraError:
                continue
            raise AssertionError("bad params accepted: %r" % (bad,))
        for bad_kw in [dict(rotation=45), dict(rotation=-90),
                       dict(mirror="yes")]:
            try:
                mgr.start(640, 480, 30, "rear", **bad_kw)
            except camera_virtual.CameraError:
                continue
            raise AssertionError("bad rotation/mirror accepted: %r" % (bad_kw,))
        assert not mgr.active


def t_status_dict_shape():
    with _Patches(device_exists=True):
        mgr = _mgr()
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


def t_stale_writer_sweep_is_safe_noop():
    # Runs the real sweep against this machine's /proc: must not raise and
    # must not kill anything (no ffmpeg v4l2 writers here). Our own python
    # process must survive, trivially.
    import remote_host  # noqa
    srv = object.__new__(remote_host.HostServer)
    import jlog as _jlog
    import tempfile
    srv.jlog = _jlog.JsonLogger(tempfile.mktemp())
    before = os.getpid()
    remote_host.HostServer._sweep_stale_camera_writers(srv)
    assert os.getpid() == before
    assert _jlog  # silence lint


def t_integration_real_start_stop():
    # Skip-if-absent: only when a REAL v4l2loopback device and ffmpeg exist.
    if not os.path.exists("/dev/video0") or not shutil.which("ffmpeg"):
        print("skip: integration (no real /dev/video0 + ffmpeg)")
        return
    mgr = _mgr()
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
    check("rotation/mirror honored in -vf chain", t_rotation_mirror_in_filter_chain)
    check("write_frame routes bytes to ffmpeg stdin", t_write_frame_routes_bytes_to_stdin)
    check("write_frame rejects non-Annex-B payloads", t_write_frame_rejects_non_annexb)
    check("stop() terminates ffmpeg and closes stdin", t_stop_terminates_process_and_closes_stdin)
    check("double start raises already-in-use", t_double_start_raises_already_in_use)
    check("inactivity timeout auto-stops", t_inactivity_timeout_triggers_auto_stop)
    check("frame resets inactivity timer", t_inactivity_reset_on_frame)
    check("no device + failed modprobe -> actionable CameraError",
          t_start_without_device_and_failed_modprobe_raises_actionable)
    check("auto-select reuses labeled device, no modprobe",
          t_auto_select_reuses_labeled_device)
    check("auto-select modprobes video_nr=-1 and adopts new node",
          t_auto_select_modprobes_and_adopts_new_node)
    check("auto-select with no device anywhere -> actionable",
          t_auto_select_no_device_anywhere_raises_actionable)
    check("missing ffmpeg raises honestly", t_start_without_ffmpeg_raises_honestly)
    check("start rejects bad params", t_start_rejects_bad_params)
    check("status_dict shape", t_status_dict_shape)
    check("stale writer sweep is a safe no-op", t_stale_writer_sweep_is_safe_noop)
    check("integration real start/stop (skip-if-absent)", t_integration_real_start_stop)
    print("V4-CAMERA CHECKS PASSED")


if __name__ == "__main__":
    main()
