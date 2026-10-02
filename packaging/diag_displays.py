#!/usr/bin/env python3
"""Test all X displays and report which has desktop content."""
import os, sys, glob
sys.path.insert(0, '/opt/remote/lib')
sys.path.insert(0, '/opt/remote/vendor')

socks = sorted(glob.glob('/tmp/.X11-unix/X*'))
print("X sockets: %s" % socks)

for sock in socks:
    disp = ":%s" % sock.split('X')[-1]
    os.environ['DISPLAY'] = disp
    # Force fresh mss connection
    try:
        import mss
        # Need fresh instance per display
        import importlib
        if 'mss' in sys.modules:
            del sys.modules['mss']
            del sys.modules['mss.linux']
        import mss as mss_mod
        with mss_mod.MSS() as s:
            mon = s.monitors[1]
            print("\n=== DISPLAY %s ===" % disp)
            print("  size: %dx%d" % (mon['width'], mon['height']))
            shot = s.grab(mon)
            # Sample pixels from center and corners
            from PIL import Image
            img = Image.frombytes("RGB", shot.size, shot.rgb)
            w, h = img.size
            samples = [
                img.getpixel((w//2, h//2)),
                img.getpixel((10, 10)),
                img.getpixel((w-10, 10)),
                img.getpixel((10, h-10)),
                img.getpixel((w-10, h-10)),
            ]
            print("  sample pixels: %s" % samples)
            # Check if all black (empty X server)
            if all(p == (0,0,0) for p in samples):
                print("  -> BLANK (all black, probably empty)")
            else:
                print("  -> HAS CONTENT (not blank)")
    except Exception as e:
        print("\n=== DISPLAY %s ===" % disp)
        print("  FAILED: %s" % e)
