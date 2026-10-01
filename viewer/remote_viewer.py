#!/usr/bin/env python3
"""remote-viewer: the controlling side of Remote.

GUI (tkinter): connect dialog for Tailscale IP/hostname + password, live
screen display, mouse/keyboard forwarding, fullscreen toggle.

Headless self-test (--self-test): connects, authenticates, receives N
frames, sends synthetic input events, checks PING/PONG, exits 0 on success.
"""
import argparse
import json
import logging
import os
import queue
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "common"))

import remote_proto as proto

LOG = logging.getLogger("remote-viewer")


class ViewerClient:
    """Protocol-level client shared by GUI and headless modes."""

    def __init__(self, host, port, password):
        self.sock = socket.create_connection((host, port), timeout=15)
        proto.client_handshake(self.sock, password, device={
            "device_id": "linux-viewer-%s" % socket.gethostname(),
            "device_name": socket.gethostname(),
            "platform": "linux",
        })
        self.frames = 0
        self.running = True

    def send_input(self, ev: dict) -> None:
        proto.send_msg(self.sock, proto.INPUT, json.dumps(ev).encode())

    def ping(self) -> bool:
        token = os.urandom(8)
        proto.send_msg(self.sock, proto.PING, token)
        deadline = time.time() + 10
        while time.time() < deadline:
            mtype, payload = proto.recv_msg(self.sock)
            if mtype == proto.PONG and payload == token:
                return True
            if mtype == proto.FRAME:
                self.frames += 1
        return False

    def recv_frame(self, timeout=15):
        self.sock.settimeout(timeout)
        try:
            while True:
                mtype, payload = proto.recv_msg(self.sock)
                if mtype == proto.FRAME:
                    self.frames += 1
                    return payload
                # ignore anything else while waiting for frames
        finally:
            self.sock.settimeout(None)

    def close(self):
        self.running = False
        try:
            proto.send_msg(self.sock, proto.DISCONNECT)
        except OSError:
            pass
        self.sock.close()


def headless_self_test(host, port, password, nframes):
    """Connect, auth, receive frames, send input, ping. Exit code 0/1."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        client = ViewerClient(host, port, password)
    except proto.AuthError as exc:
        print("SELFTEST FAIL: auth: %s" % exc)
        return 1
    except OSError as exc:
        print("SELFTEST FAIL: connect: %s" % exc)
        return 1
    print("SELFTEST: authenticated")
    try:
        for i in range(nframes):
            frame = client.recv_frame()
            if not (frame[:2] == b"\xff\xd8" and frame[-2:] == b"\xff\xd9"):
                print("SELFTEST FAIL: frame %d is not a JPEG (%d bytes)" % (i, len(frame)))
                return 1
        print("SELFTEST: received %d valid JPEG frames" % nframes)
        for ev in ({"t": "move", "x": 10, "y": 20},
                   {"t": "click", "button": "left", "down": True},
                   {"t": "click", "button": "left", "down": False},
                   {"t": "scroll", "dx": 0, "dy": -1},
                   {"t": "key", "key": "a", "down": True},
                   {"t": "key", "key": "a", "down": False}):
            client.send_input(ev)
        print("SELFTEST: sent 6 input events")
        time.sleep(1.5)  # let the host process them before we disconnect
        if not client.ping():
            print("SELFTEST FAIL: ping/pong")
            return 1
        print("SELFTEST: ping/pong ok")
    finally:
        client.close()
    print("SELFTEST: PASS")
    return 0


# ---------------------------------------------------------------- GUI

class ViewerGUI:
    def __init__(self, root, client):
        import tkinter as tk
        from PIL import Image, ImageTk
        self._tk = tk
        self._Image, self._ImageTk = Image, ImageTk
        self.root = root
        self.client = client
        self.frame_q = queue.Queue(maxsize=2)
        self.host_w, self.host_h = 0, 0
        self.disp_w, self.disp_h = 0, 0
        self.fullscreen = False

        root.title("Remote")
        self.label = tk.Label(root, bg="black")
        self.label.pack(fill=tk.BOTH, expand=True)
        root.bind("<F11>", lambda e: self.toggle_fullscreen())
        root.bind("<Escape>", lambda e: self.leave_fullscreen() if self.fullscreen else None)
        self.label.bind("<Motion>", self.on_move)
        self.label.bind("<ButtonPress>", lambda e: self.on_button(e, True))
        self.label.bind("<ButtonRelease>", lambda e: self.on_button(e, False))
        self.label.bind("<Button-4>", lambda e: self.on_scroll(e, -1))
        self.label.bind("<Button-5>", lambda e: self.on_scroll(e, 1))
        self.label.bind("<MouseWheel>", self.on_wheel)
        self.label.bind("<KeyPress>", lambda e: self.on_key(e, True))
        self.label.bind("<KeyRelease>", lambda e: self.on_key(e, False))
        self.label.focus_set()

        self.net_thread = threading.Thread(target=self._net_loop, daemon=True)
        self.net_thread.start()
        self._pump_frames()

    def toggle_fullscreen(self):
        self.fullscreen = not self.fullscreen
        self.root.attributes("-fullscreen", self.fullscreen)

    def leave_fullscreen(self):
        self.fullscreen = False
        self.root.attributes("-fullscreen", False)

    def _to_host(self, x, y):
        if not self.host_w:
            return 0, 0
        return int(x * self.host_w / self.disp_w), int(y * self.host_h / self.disp_h)

    def _send(self, ev):
        try:
            self.client.send_input(ev)
        except OSError:
            pass

    def on_move(self, e):
        x, y = self._to_host(e.x, e.y)
        self._send({"t": "move", "x": x, "y": y})

    def on_button(self, e):
        x, y = self._to_host(e.x, e.y)
        btn = {1: "left", 2: "middle", 3: "right"}.get(e.num, "left")
        self._send({"t": "click", "button": btn, "down": e.type == "4"})  # ButtonPress=4
        return "break"

    def on_scroll(self, e, direction):
        self._send({"t": "scroll", "dx": 0, "dy": direction})

    def on_wheel(self, e):
        self._send({"t": "scroll", "dx": 0, "dy": -1 if e.delta > 0 else 1})

    def on_key(self, e):
        self._send({"t": "key", "key": e.keysym, "down": e.type == "2"})  # KeyPress=2
        return "break"

    def _net_loop(self):
        import io
        while self.client.running:
            try:
                data = self.client.recv_frame(timeout=30)
            except (proto.ProtocolError, OSError):
                break
            try:
                img = self._Image.open(io.BytesIO(data))
                img.load()
            except Exception:  # noqa: BLE001 - corrupt frame, skip
                continue
            try:
                self.frame_q.put_nowait(img)
            except queue.Full:
                pass

    def _pump_frames(self):
        try:
            while True:
                img = self.frame_q.get_nowait()
                w = self.label.winfo_width() or img.width
                h = self.label.winfo_height() or img.height
                self.host_w, self.host_h = img.width, img.height
                # fit to window keeping aspect
                scale = min(w / img.width, h / img.height)
                dw, dh = max(1, int(img.width * scale)), max(1, int(img.height * scale))
                self.disp_w, self.disp_h = dw, dh
                photo = self._ImageTk.PhotoImage(img.resize((dw, dh)))
                self.label.configure(image=photo)
                self.label.image = photo  # keep a reference
        except queue.Empty:
            pass
        if self.client.running:
            self.root.after(33, self._pump_frames)
        else:
            self.root.title("Remote - disconnected")


def run_gui(host, port, password):
    import tkinter as tk
    from tkinter import simpledialog
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    root = tk.Tk()
    root.withdraw()
    host = host or simpledialog.askstring("Remote", "Tailscale IP or hostname:",
                                         parent=root) or ""
    password = password or simpledialog.askstring("Remote", "Password:",
                                                 show="*", parent=root) or ""
    root.deiconify()
    if not host:
        return 1
    try:
        client = ViewerClient(host, port, password)
    except (proto.AuthError, OSError) as exc:
        from tkinter import messagebox
        messagebox.showerror("Remote", "Connection failed: %s" % exc)
        return 1
    root.geometry("1280x800")
    ViewerGUI(root, client)
    try:
        root.mainloop()
    finally:
        client.close()
    return 0


def main():
    ap = argparse.ArgumentParser(description="remote-viewer: Remote controlling side")
    ap.add_argument("--host", default="", help="Tailscale IP/hostname of the host")
    ap.add_argument("--port", type=int, default=proto.PORT)
    ap.add_argument("--password", default="", help="host password (prompted in GUI mode)")
    ap.add_argument("--self-test", action="store_true",
                    help="headless client test: auth, frames, input, ping")
    ap.add_argument("--frames", type=int, default=20)
    args = ap.parse_args()
    if args.self_test:
        return headless_self_test(args.host or "127.0.0.1", args.port,
                                  args.password, args.frames)
    return run_gui(args.host, args.port, args.password)


if __name__ == "__main__":
    sys.exit(main())
