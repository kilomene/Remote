#!/usr/bin/env python3
"""host/tray.py -- best-effort system tray / desktop notifications for Remote.

Backend preference: pystray -> PyGObject StatusIcon -> none. Every import
is guarded; a headless machine (no DISPLAY/WAYLAND_DISPLAY) gets a clean
no-op TrayManager whose methods simply do nothing. Nothing here ever raises
because of a missing GUI stack -- the host must stay up with or without a
tray.

notify(title, body) additionally tries `notify-send` as a best effort, even
when no tray backend loaded (it may still work inside a desktop session).
"""
import os
import shutil
import subprocess
import sys


def _log(msg):
    print("tray: %s" % msg, file=sys.stderr)


def have_display():
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _try_pystray():
    try:
        import pystray
    except ImportError:
        return None
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        _log("pystray present but Pillow missing; skipping pystray")
        return None
    return (pystray, Image, ImageDraw)


def _try_statusicon():
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk
    except (ImportError, ValueError):
        return None
    return Gtk


class TrayManager:
    """Desktop tray icon + notifications. Safe no-op when unavailable."""

    def __init__(self):
        self.available = False
        self.backend = None
        self._icon = None
        if not have_display():
            _log("no DISPLAY/WAYLAND_DISPLAY: tray disabled (headless no-op)")
            return
        mods = _try_pystray()
        if mods is not None:
            self.backend = "pystray"
            self._mods = mods
            self.available = True
            _log("using pystray backend")
            return
        Gtk = _try_statusicon()
        if Gtk is not None:
            self.backend = "statusicon"
            self._Gtk = Gtk
            self.available = True
            _log("using Gtk StatusIcon backend")
            return
        _log("no tray backend available (install pystray or PyGObject); "
             "tray disabled")

    # -- public API -----------------------------------------------------
    def set_status(self, connected_count, network):
        """Update the tray tooltip/icon. No-op when unavailable."""
        if not self.available:
            return
        try:
            self._set_status_real(int(connected_count), str(network))
        except Exception as e:  # never let the tray break the host
            _log("set_status failed: %s" % e)

    def notify(self, title, body):
        """Desktop notification; best effort via `notify-send`."""
        if not shutil.which("notify-send"):
            _log("notify-send not found; notification dropped: %s" % title)
            return
        try:
            subprocess.run(["notify-send", "Remote: " + str(title), str(body)],
                           timeout=10, capture_output=True)
        except (OSError, subprocess.SubprocessError) as e:
            _log("notify-send failed: %s" % e)

    def run(self):
        """Block serving the tray icon (call from a thread). No-op if none."""
        if not self.available:
            return
        try:
            self._run_real()
        except Exception as e:
            _log("tray run failed: %s" % e)

    def stop(self):
        if not self.available:
            return
        try:
            if self.backend == "pystray" and self._icon is not None:
                self._icon.stop()
            elif self.backend == "statusicon":
                self._Gtk.main_quit()
        except Exception as e:
            _log("tray stop failed: %s" % e)

    # -- backend implementations ----------------------------------------
    def _make_icon_image(self, connected):
        pystray, Image, ImageDraw = self._mods
        color = (46, 160, 67) if connected else (110, 118, 129)
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse([8, 8, 56, 56], fill=color)
        return img

    def _set_status_real(self, connected_count, network):
        if self.backend == "pystray":
            pystray = self._mods[0]
            if self._icon is None:
                self._icon = pystray.Icon(
                    "remote",
                    self._make_icon_image(connected_count > 0),
                    "Remote")
            self._icon.icon = self._make_icon_image(connected_count > 0)
            self._icon.title = ("Remote: %d connected (%s)"
                                % (connected_count, network))
        elif self.backend == "statusicon":
            if self._icon is None:
                self._icon = self._Gtk.StatusIcon()
            self._icon.set_tooltip_text("Remote: %d connected (%s)"
                                        % (connected_count, network))
            self._icon.set_visible(True)

    def _run_real(self):
        if self.backend == "pystray":
            if self._icon is None:
                pystray = self._mods[0]
                self._icon = pystray.Icon("remote",
                                          self._make_icon_image(False),
                                          "Remote")
            self._icon.run()
        elif self.backend == "statusicon":
            self._Gtk.main()
