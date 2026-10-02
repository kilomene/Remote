#!/usr/bin/env python3
"""yourremote-pair-code: issue a one-time pairing code for a new device.

Uses host/auth.py's PairingManager (documented interface):

    PairingManager(path).issue_code(device_name) -> str   # the code

If auth.py is not installed yet, this exits with a clear error instead of
a traceback. When host/qr.py exists and exposes write_png(text, path),
a QR PNG of the code is also written and its path printed.

Installed as /usr/bin/yourremote-pair-code.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import auth
    _HAVE_AUTH = True
    _AUTH_IMPORT_ERROR = None
    DEFAULT_STORE = getattr(auth, "DEFAULT_PAIRING_PATH",
                            "/etc/remote/pairing.json")
except ImportError as e:
    _HAVE_AUTH = False
    _AUTH_IMPORT_ERROR = e
    DEFAULT_STORE = "/etc/remote/pairing.json"

try:
    import qr as qr_mod
    _HAVE_QR = hasattr(qr_mod, "make_pairing_png")
except ImportError:
    qr_mod = None
    _HAVE_QR = False

try:
    from yourremote_settings import get_device_id as _get_device_id
except ImportError:
    _get_device_id = None


def issue(device_name, store=DEFAULT_STORE):
    """Issue a code via PairingManager. Raises SystemExit with a clear
    message when auth.py is missing or the interface is wrong."""
    if not _HAVE_AUTH:
        raise SystemExit(
            "ERROR: host/auth.py (PairingManager) is not installed yet "
            "(%s). Pairing codes need the auth workstream's module."
            % _AUTH_IMPORT_ERROR)
    try:
        mgr = auth.PairingManager(store)
        issue_fn = mgr.issue_code
    except AttributeError as e:
        raise SystemExit(
            "ERROR: host/auth.py does not expose the documented interface "
            "PairingManager(path).issue_code(device_name) -> str: %s" % e)
    try:
        code = issue_fn(device_name)
    except OSError as e:
        raise SystemExit(
            "ERROR: could not save the pairing code (%s).\n"
            "Run with sudo so it can write the pairing store." % e)
    if not isinstance(code, str) or not code.strip():
        raise SystemExit("ERROR: PairingManager.issue_code() did not return "
                         "a code string")
    return code.strip()


def maybe_qr(code):
    """Write a QR PNG via host/qr.py. Returns the path or None.

    Uses make_pairing_qr(code, device_id) (pairing URI) when a device id
    is obtainable, else make_pairing_png(code) (raw code). Both return PNG
    bytes; QrUnavailable (missing vendored qrcode wheel) is handled
    gracefully.
    """
    if not _HAVE_QR:
        return None
    out = "/tmp/remote-pair-%s.png" % "".join(
        c for c in code if c.isalnum())[:32]
    try:
        if _get_device_id is not None and hasattr(qr_mod, "make_pairing_qr"):
            try:
                png = qr_mod.make_pairing_qr(code, _get_device_id())
            except OSError:
                png = qr_mod.make_pairing_png(code)
        else:
            png = qr_mod.make_pairing_png(code)
        with open(out, "wb") as f:
            f.write(png)
        return out
    except Exception as e:
        print("warning: QR generation failed: %s" % e, file=sys.stderr)
        return None


def main():
    ap = argparse.ArgumentParser(
        description="issue a one-time pairing code for a new Remote device")
    ap.add_argument("device_name", help="friendly name for the new device")
    ap.add_argument("--store", default=DEFAULT_STORE,
                    help="pairing store path (default %(default)s)")
    args = ap.parse_args()

    code = issue(args.device_name, store=args.store)
    print()
    print("  Pairing code for %r:" % args.device_name)
    print()
    print("      %s" % code)
    print()
    print("  Enter this code in the Remote viewer to pair.")
    qr_path = maybe_qr(code)
    if qr_path:
        print("  QR code: %s" % qr_path)
    elif qr_mod is None:
        print("  (no QR: host/qr.py not installed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
