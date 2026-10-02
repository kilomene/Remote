#!/usr/bin/env python3
"""QR pairing codes for the Remote host.

make_pairing_png(payload_text) -> PNG bytes encoding a REAL QR code for the
pairing URI, e.g. "yourremote://pair?code=123456&id=YR-ABCD-1234".

The QR encoding itself comes from the vendored `qrcode` pure-python wheel
(packaging/vendor/qrcode-*.whl -> /opt/remote/vendor); the matrix is
rasterized to PNG with a minimal stdlib (zlib/struct) 1-bit PNG writer, so
no Pillow is required. This is a genuine QR code, not a drawn fake.

The manual code entry remains the fallback flow: if qrcode is unavailable,
make_pairing_png raises QrUnavailable and the caller degrades to showing
the 6-digit code as text.
"""
import logging
import os
import struct
import sys
import zlib

LOG = logging.getLogger("remote-qr")


class QrUnavailable(Exception):
    """Raised when no QR backend is available (manual entry is the fallback)."""


def _ensure_vendor():
    """Make vendored pure-python wheels importable (same scheme as
    remote_host.ensure_vendor). Wheels ship as unpacked trees in the .deb
    (/opt/remote/vendor); in a dev checkout they may still be .whl zips,
    which are importable directly via zipimport."""
    for d in (os.environ.get("REMOTE_VENDOR_DIR"), "/opt/remote/vendor"):
        if not d or not os.path.isdir(d):
            continue
        if d not in sys.path:
            sys.path.insert(0, d)
        try:
            for name in os.listdir(d):
                if name.endswith(".whl"):
                    whl = os.path.join(d, name)
                    if whl not in sys.path:
                        sys.path.insert(0, whl)
        except OSError:
            pass


def pairing_uri(code, device_id):
    """The payload text encoded in the QR: a yourremote:// pair URI."""
    return "yourremote://pair?code=%s&id=%s" % (code, device_id)


def _png_chunk(ctype, data):
    chunk = ctype + data
    return struct.pack(">I", len(data)) + chunk + \
        struct.pack(">I", zlib.crc32(chunk) & 0xFFFFFFFF)


def _matrix_to_png(matrix, box=8, border=4):
    """Rasterize a QR module matrix (list of lists of bool) to 1-bit PNG."""
    mods = len(matrix)
    size = mods + 2 * border
    px = size * box
    # 1-bit grayscale: each scanline = filter byte + ceil(px/8) bytes
    rowbytes = (px + 7) // 8
    rows = bytearray()
    # In PNG color type 0 bit depth 1: sample 0 = black, 1 = white.
    for my in range(size):
        line = bytearray(1 + rowbytes)  # filter byte 0 (None) + packed bits
        for mx in range(size):
            if border <= my < border + mods and border <= mx < border + mods \
                    and matrix[my - border][mx - border]:
                continue  # module on -> black (bit already 0)
            for b in range(box):  # off / quiet zone -> white (bit 1)
                x = mx * box + b
                line[1 + x // 8] |= 0x80 >> (x % 8)
        rows.extend(line * box)  # vertical box scaling
    ihdr = struct.pack(">IIBBBBB", px, px, 1, 0, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"IDAT", zlib.compress(bytes(rows), 9))
            + _png_chunk(b"IEND", b""))


def make_pairing_png(payload_text, box=8, border=4):
    """Encode payload_text as a QR code and return PNG bytes.

    Raises QrUnavailable if the vendored qrcode wheel cannot be imported.
    """
    _ensure_vendor()
    try:
        import qrcode
    except ImportError as exc:
        raise QrUnavailable("qrcode wheel not available: %s" % exc)
    qr = qrcode.QRCode(border=0, box_size=1)  # border handled at raster time
    qr.add_data(payload_text)
    qr.make(fit=True)
    return _matrix_to_png(qr.get_matrix(), box=box, border=border)


def make_pairing_qr(code, device_id):
    """Convenience: PNG bytes for the pairing URI of (code, device_id)."""
    return make_pairing_png(pairing_uri(code, device_id))
