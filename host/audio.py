"""host/audio.py -- real Linux audio capture for Remote v4.

Protocol v4 media numbers (canonical):
  AUDIO_START 0x70 (c->s JSON {"source": "<id>"})
  AUDIO_DATA  0x71 (s->c raw bytes, see WIRE FORMAT below)
  AUDIO_STOP  0x72 (either side, empty payload)
  AUDIO_ERROR 0x73 (s->c JSON {"detail": "<message>"})

Permission flags "audio"/"webcam" are enforced by the host integrator;
this module is the capture engine only.

WIRE FORMAT (AUDIO_DATA payload, host -> Android player)
--------------------------------------------------------
Every payload is: MAGIC(4 bytes) + RATE(u32 big-endian) + CHANNELS(u32
big-endian) + media bytes. That is a 12-byte self-describing header on
every frame, so the Android player can decode honestly without side
information.

MAGIC is one of:
  b"PCM1" -- media bytes are raw 16-bit signed PCM, little-endian,
              interleaved by channel. rate is normally 48000, channels 2.
              Byte count of the media part is always rate*channels*2*seconds.
  b"OPUS1" -- media bytes are an Ogg-Opus stream fragment produced by a
              real `opusenc` encode of the same PCM. Only emitted when the
              `opusenc` binary is present on the host.

The host picks Opus when `opusenc` exists (much smaller for remote
streaming) and falls back to PCM1 otherwise. The Android player MUST
branch on the magic; both are real, non-fake encodings.

CAPTURE
-------
  monitor sources (system playback loopback):
      parec --monitor-stream=<index> --format=s16le --rate=48000 --channels=2
  PipeWire sources:
      pw-record --target <id> --format s16 --rate 48000 --channels 2 -

If neither PulseAudio (pactl/parec) nor PipeWire (pw-cli/pw-record) exists,
list_sources() returns [] and start() raises AudioError -- graceful
degradation, never a fake stream.
"""
import logging
import os
import re
import select
import shutil
import struct
import subprocess
import threading

LOG = logging.getLogger("remote-host")

RATE = 48000
CHANNELS = 2
SAMPLE_WIDTH = 2  # s16le
PCM1_MAGIC = b"PCM1"
OPUS1_MAGIC = b"OPUS1"
HEADER_FMT = ">4sII"  # magic, rate, channels
HEADER_LEN = struct.calcsize(HEADER_FMT)  # 12


class AudioError(Exception):
    pass


def opus_available():
    """True when a real Opus encoder binary is on PATH."""
    return shutil.which("opusenc") is not None


def encode_chunk(pcm_bytes, rate=RATE, channels=CHANNELS, use_opus=None):
    """Prepend the 12-byte v4 wire header to PCM audio bytes.

    use_opus=None selects the real system state: OPUS1 magic if `opusenc`
    exists (bytes must be genuine opusenc output), PCM1 otherwise. Pass
    use_opus=False to force the PCM1 path (e.g. on a host without opusenc).
    use_opus=True is an error here -- this function cannot fake an Opus
    encode; Opus bytes only ever come from the live opusenc subprocess.
    """
    if use_opus is None:
        use_opus = opus_available()
    if use_opus:
        raise AudioError("encode_chunk cannot synthesize Opus bytes; "
                         "they only come from a live opusenc subprocess")
    if not isinstance(pcm_bytes, (bytes, bytearray)) or len(pcm_bytes) == 0:
        raise AudioError("encode_chunk got empty/non-bytes audio")
    return struct.pack(HEADER_FMT, PCM1_MAGIC, rate, channels) + bytes(pcm_bytes)


def decode_header(frame):
    """Inverse of encode_chunk for tests/integrators. Returns
    (magic, rate, channels, media_bytes)."""
    if len(frame) < HEADER_LEN:
        raise AudioError("frame shorter than 12-byte header")
    magic, rate, channels = struct.unpack(HEADER_FMT, frame[:HEADER_LEN])
    return magic, rate, channels, frame[HEADER_LEN:]


# -- source enumeration -------------------------------------------------------

def _pactl_sources():
    """Parse `pactl list sources`. Monitor sources (kind='monitor') are the
    system-playback loopback; everything else is a mic (kind='mic')."""
    try:
        proc = subprocess.run(["pactl", "list", "sources"],
                              capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    text = proc.stdout.decode("utf-8", "replace")
    sources = []
    cur = {}
    for line in text.splitlines():
        m = re.match(r"^Source #(\d+)", line)
        if m:
            if cur.get("name"):
                sources.append(cur)
            cur = {"index": m.group(1)}
        else:
            m = re.match(r"^\s*Name:\s*(\S+)", line)
            if m:
                cur["name"] = m.group(1)
            else:
                m = re.match(r"^\s*Description:\s*(.+)", line)
                if m:
                    cur["desc"] = m.group(1).strip()
    if cur.get("name"):
        sources.append(cur)
    out = []
    for s in sources:
        name = s["name"]
        kind = "monitor" if name.endswith(".monitor") else "mic"
        out.append({"id": "pulse:%s" % name,
                    "name": s.get("desc") or name,
                    "kind": kind,
                    "_pulse_name": name,
                    "_pulse_index": s.get("index")})
    return out


def _pipewire_sources():
    """Parse `pw-cli list-objects Node`. Only real nodes are listed; on
    parse failure or absence the function returns []."""
    try:
        proc = subprocess.run(["pw-cli", "list-objects", "Node"],
                              capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    text = proc.stdout.decode("utf-8", "replace")
    sources = []
    cur = {}
    for line in text.splitlines():
        m = re.match(r"^\s*id:\s*(\d+),", line)
        if m:
            if cur.get("id"):
                sources.append(cur)
            cur = {"id": m.group(1)}
            continue
        m = re.match(r'^\s*(?:node\.name|node\.description|port\.alias)\s*=\s*"([^"]+)"', line)
        if m:
            key = line.split("=")[0].strip()
            if key == "node.name":
                cur["node_name"] = m.group(1)
            elif key == "node.description":
                cur["desc"] = m.group(1)
            elif key == "port.alias" and "desc" not in cur:
                cur["desc"] = m.group(1)
    if cur.get("id"):
        sources.append(cur)
    out = []
    for s in sources:
        node = s.get("node_name", "")
        if not node:
            continue
        kind = "monitor" if "monitor" in node.lower() else "mic"
        out.append({"id": "pipewire:%s" % s["id"],
                    "name": s.get("desc") or node,
                    "kind": kind,
                    "_pw_id": s["id"]})
    return out


def list_sources():
    """Return real audio sources. [] when no audio stack exists (headless)."""
    return _pactl_sources() + _pipewire_sources()


# -- capture ------------------------------------------------------------------

class AudioCapture:
    """Subprocess-based capture. One active stream at a time."""

    def __init__(self, rate=RATE, channels=CHANNELS, seconds=0.1):
        self.rate = int(rate)
        self.channels = int(channels)
        self.seconds = float(seconds)
        self._proc = None          # parec / pw-record
        self._encoder = None       # opusenc (optional)
        self._enc_thread = None
        self._stop = threading.Event()
        self._source = None
        self._lock = threading.Lock()
        self._use_opus = False

    # -- lifecycle ---------------------------------------------------------

    def start(self, source_id):
        """Begin capturing from source_id (as returned by list_sources).
        Raises AudioError with an honest message on any failure."""
        with self._lock:
            if self._proc is not None:
                raise AudioError("capture already running")
            src = None
            for s in list_sources():
                if s["id"] == source_id:
                    src = s
                    break
            if src is None:
                raise AudioError("unknown audio source: %r (run list_sources)" % source_id)
            cmd = self._capture_cmd(src)
            try:
                self._proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            except OSError as exc:
                raise AudioError("failed to spawn %s: %s" % (cmd[0], exc))
            self._source = src
            if opus_available():
                try:
                    self._encoder = subprocess.Popen(
                        ["opusenc", "--raw",
                         "--raw-rate", str(self.rate),
                         "--raw-chan", str(self.channels),
                         "--framesize", "20", "-", "-"],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL)
                    self._enc_thread = threading.Thread(
                        target=self._pump_to_encoder, daemon=True)
                    self._enc_thread.start()
                    self._use_opus = True
                except OSError as exc:
                    LOG.warning("opusenc spawn failed, staying on PCM: %s", exc)
                    self._encoder = None
            LOG.info("audio capture started: %s (%s)", source_id,
                     "opus" if self._use_opus else "pcm")

    def _capture_cmd(self, src):
        kind = src.get("kind")
        if src["id"].startswith("pulse:") and shutil.which("parec"):
            args = ["parec", "--format=s16le", "--rate=%d" % self.rate,
                    "--channels=%d" % self.channels]
            if kind == "monitor" and src.get("_pulse_index") is not None:
                args.append("--monitor-stream=%s" % src["_pulse_index"])
            elif src.get("_pulse_name"):
                args.append("--device=%s" % src["_pulse_name"])
            return args
        if src["id"].startswith("pipewire:") and shutil.which("pw-record"):
            return ["pw-record", "--target", src["_pw_id"],
                    "--format", "s16", "--rate", str(self.rate),
                    "--channels", str(self.channels), "-"]
        raise AudioError("no capture binary available for source %r" % src["id"])

    def _pump_to_encoder(self):
        """Feed capture stdout into opusenc stdin until stopped."""
        try:
            while not self._stop.is_set() and self._proc and self._encoder:
                chunk = self._proc.stdout.read(8192)
                if not chunk:
                    break
                try:
                    self._encoder.stdin.write(chunk)
                    self._encoder.stdin.flush()
                except (OSError, BrokenPipeError):
                    break
        finally:
            try:
                if self._encoder and self._encoder.stdin:
                    self._encoder.stdin.close()
            except OSError:
                pass

    # -- streaming ----------------------------------------------------------

    def read_chunk(self, seconds=None):
        """Read one audio chunk and return it with the 12-byte v4 header.
        Raises AudioError if not started or the capture subprocess died."""
        seconds = self.seconds if seconds is None else float(seconds)
        want = int(self.rate * self.channels * SAMPLE_WIDTH * seconds)
        if want <= 0:
            raise AudioError("bad chunk length: %r" % seconds)
        with self._lock:
            proc, encoder = self._proc, self._encoder
        if proc is None:
            raise AudioError("capture not started")
        if proc.poll() is not None:
            raise AudioError("capture subprocess exited (code %s)" % proc.poll())
        if self._use_opus:
            if encoder is None or encoder.poll() is not None:
                raise AudioError("opus encoder died")
            data = self._read_from(encoder.stdout, want)
            magic = OPUS1_MAGIC
        else:
            data = self._read_from(proc.stdout, want)
            magic = PCM1_MAGIC
        return struct.pack(HEADER_FMT, magic, self.rate, self.channels) + data

    @staticmethod
    def _read_from(stream, want):
        buf = bytearray()
        while len(buf) < want:
            ready, _, _ = select.select([stream], [], [], 5.0)
            if not ready:
                raise AudioError("audio read timed out")
            chunk = os.read(stream.fileno(), want - len(buf))
            if not chunk:
                raise AudioError("audio stream ended early")
            buf += chunk
        return bytes(buf)

    def stop(self):
        """Terminate capture and encoder subprocesses. No zombies left."""
        with self._lock:
            proc, encoder = self._proc, self._encoder
            self._proc = None
            self._encoder = None
            self._source = None
            self._use_opus = False
        self._stop.set()
        for p, name in ((encoder, "opusenc"), (proc, "capture")):
            if p is None:
                continue
            try:
                p.terminate()
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    p.kill()
                    p.wait(timeout=3)
                except OSError:
                    pass
            except OSError:
                pass
            finally:
                for fh in (p.stdout, p.stdin, p.stderr):
                    try:
                        if fh:
                            fh.close()
                    except OSError:
                        pass
        if self._enc_thread is not None:
            self._enc_thread.join(timeout=3)
            self._enc_thread = None
        self._stop.clear()

    @property
    def running(self):
        with self._lock:
            return self._proc is not None and self._proc.poll() is None
