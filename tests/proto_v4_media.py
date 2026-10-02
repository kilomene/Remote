#!/usr/bin/env python3
"""Independent v4 media conformance check for Remote audio/webcam engines.

Exits 0 and prints "V4-MEDIA CHECKS PASSED" on success. Every check is
honest about the headless test box: list_sources()/list_cameras() must
degrade to [] without exceptions; PCM1 header + chunk sizes are verified
through the REAL encoder-selection path (forcing use_opus=False when
opusenc is absent); grab_frame() must raise WebcamError (never return a
fake image); start() on a bogus source must raise AudioError.

No fakes: nothing here synthesizes a "captured" frame or a "captured"
audio stream and calls it real.
"""
import math
import os
import shutil
import struct
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "host"))

import audio      # noqa: E402
import webcam     # noqa: E402


def check(name, fn):
    fn()
    print("ok: %s" % name)


def t_list_sources_degrades():
    # Headless box has no pactl/pw-cli -> must be [] and must not raise.
    if shutil.which("pactl") or shutil.which("pw-cli"):
        sources = audio.list_sources()
        assert isinstance(sources, list), type(sources)
        return
    assert audio.list_sources() == []


def t_list_cameras_degrades():
    # Headless box has no /sys/class/video4linux -> must be [] and not raise.
    if os.path.isdir(webcam.SYSFS):
        cams = webcam.list_cameras()
        assert isinstance(cams, list), type(cams)
        return
    assert webcam.list_cameras() == []


def t_pcm1_header_and_chunks():
    # 440 Hz sine, 0.1 s @ 48 kHz stereo s16le -> real encode_chunk path.
    rate, chans = audio.RATE, audio.CHANNELS
    n = int(rate * 0.1)
    pcm = bytearray()
    for i in range(n):
        s = int(32767 * 0.5 * math.sin(2 * math.pi * 440 * i / rate))
        pcm += struct.pack("<hh", s, s)
    pcm = bytes(pcm)
    assert len(pcm) == rate * chans * 2 * 1 // 10  # 19200 bytes

    if shutil.which("opusenc"):
        # Real system has opusenc: PCM1 path is still selectable explicitly.
        frame = audio.encode_chunk(pcm, use_opus=False)
    else:
        # Default selection must pick the honest PCM1 fallback.
        frame = audio.encode_chunk(pcm)
    magic, r, c, media = audio.decode_header(frame)
    assert magic == b"PCM1", magic
    assert r == 48000 and c == 2, (r, c)
    assert len(frame[:12]) == 12
    assert media == pcm, "media bytes must pass through untouched"
    assert len(frame) == 12 + 19200, len(frame)


def t_encode_chunk_refuses_fake_opus():
    # use_opus=True without a live encoder subprocess must not synthesize
    # Opus bytes -- the function raises instead.
    try:
        audio.encode_chunk(b"\x00\x01" * 100, use_opus=True)
    except audio.AudioError:
        return
    raise AssertionError("encode_chunk must refuse to fake Opus")


def t_grab_frame_raises_not_fakes():
    # No device: grab_frame must raise WebcamError, never invent an image.
    try:
        webcam.grab_frame("video99")
    except webcam.WebcamError as exc:
        assert "video99" in str(exc) or "no such device" in str(exc), exc
        return
    raise AssertionError("grab_frame did not raise WebcamError")


def t_grab_frame_rejects_bad_id():
    for bad in ["../etc/passwd", "video0;reboot", "", None, "0"]:
        try:
            webcam.grab_frame(bad)
        except webcam.WebcamError:
            continue
        raise AssertionError("bad device id accepted: %r" % (bad,))


def t_start_bogus_source_raises():
    cap = audio.AudioCapture()
    try:
        cap.start("bogus-source")
    except audio.AudioError:
        return
    finally:
        cap.stop()
    raise AssertionError("start() on bogus source did not raise AudioError")


def t_read_chunk_without_start_raises():
    cap = audio.AudioCapture()
    try:
        cap.read_chunk()
    except audio.AudioError:
        return
    raise AssertionError("read_chunk() before start() did not raise AudioError")


def main():
    check("list_sources degrades gracefully", t_list_sources_degrades)
    check("list_cameras degrades gracefully", t_list_cameras_degrades)
    check("pcm1 header + chunk sizes", t_pcm1_header_and_chunks)
    check("encode_chunk refuses fake opus", t_encode_chunk_refuses_fake_opus)
    check("grab_frame raises (no fake image)", t_grab_frame_raises_not_fakes)
    check("grab_frame rejects bad device ids", t_grab_frame_rejects_bad_id)
    check("start(bogus) raises AudioError", t_start_bogus_source_raises)
    check("read_chunk before start raises", t_read_chunk_without_start_raises)
    print("V4-MEDIA CHECKS PASSED")


if __name__ == "__main__":
    main()
