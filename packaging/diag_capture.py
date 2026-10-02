#!/usr/bin/env python3
"""Diagnose screen capture on the Remote host. Run as the host user."""
import os, sys
sys.path.insert(0, '/opt/remote/lib')

print("DISPLAY=%s" % os.environ.get('DISPLAY'))
print("XAUTHORITY=%s" % os.environ.get('XAUTHORITY'))

# Check X socket
import glob
socks = glob.glob('/tmp/.X11-unix/X*')
print("X sockets: %s" % socks)

# Check xauth file
xauth = os.environ.get('XAUTHORITY', os.path.expanduser('~/.Xauthority'))
print("xauth file: %s exists=%s" % (xauth, os.path.exists(xauth)))

# Try mss
try:
    sys.path.insert(0, '/opt/remote/vendor')
    import mss
    with mss.mss() as s:
        print("mss monitors: %s" % s.monitors)
        mon = s.monitors[1]
        print("monitor 1: %dx%d" % (mon['width'], mon['height']))
        img = s.grab(mon)
        print("grab OK: %dx%d, pixels=%d" % (img.width, img.height, len(img.rgb)))
        # Check if pixels are all zeros (garbage)
        data = img.rgb[:1000]
        if all(b == 0 for b in data):
            print("WARNING: pixels are all zeros - display not readable")
        else:
            print("pixels look valid (non-zero data)")
except Exception as e:
    print("mss FAILED: %s" % e)

# Try xlib
try:
    from Xlib import display as xdisplay
    d = xdisplay.Display()
    print("xlib Display OK: %s" % d.get_display_name())
    root = d.screen().root
    geom = root.get_geometry()
    print("root window: %dx%d" % (geom.width, geom.height))
except Exception as e:
    print("xlib FAILED: %s" % e)
