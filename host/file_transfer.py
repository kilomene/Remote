#!/usr/bin/env python3
"""File transfer backend for remote-host (protocol v2, 0x50-0x5A).

All paths are jailed under ``root`` (the user's home by default). Any
escape attempt (``..``, absolute-path tricks, symlink races) raises
JailError and is reported to the client as FILE_ERROR.
"""
import os
import shutil

CHUNK = 256 * 1024  # FILE_DATA chunk size


class JailError(Exception):
    pass


class FileTransfer:
    def __init__(self, root):
        self.root = os.path.realpath(os.path.expanduser(root))
        os.makedirs(self.root, exist_ok=True)

    def resolve(self, relpath):
        """Resolve a client-supplied path inside the jail."""
        if not isinstance(relpath, str) or not relpath:
            raise JailError("empty path")
        # strip leading slashes: everything is relative to the jail root
        rel = relpath.lstrip("/")
        abs_p = os.path.realpath(os.path.join(self.root, rel))
        if abs_p != self.root and not abs_p.startswith(self.root + os.sep):
            raise JailError("path escapes file root")
        return abs_p

    def list_dir(self, relpath):
        """Return (display_path, entries) for a directory listing."""
        path = self.resolve(relpath or ".")
        if not os.path.isdir(path):
            raise JailError("not a directory: %s" % relpath)
        entries = []
        with os.scandir(path) as it:
            for e in it:
                try:
                    st = e.stat(follow_symlinks=False)
                    entries.append({
                        "name": e.name,
                        "size": st.st_size,
                        "dir": e.is_dir(follow_symlinks=False),
                        "mtime": int(st.st_mtime),
                    })
                except OSError:
                    continue
        entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
        return entries

    def open_read(self, relpath, offset=0):
        """Return (size, binary file object) for a download."""
        path = self.resolve(relpath)
        if not os.path.isfile(path):
            raise JailError("not a file: %s" % relpath)
        size = os.path.getsize(path)
        if offset < 0 or offset > size:
            raise JailError("bad offset %d for size %d" % (offset, size))
        fh = open(path, "rb")
        if offset:
            fh.seek(offset)
        return size, fh

    def begin_write(self, relpath, size):
        """Open a destination for upload; parents are created as needed."""
        path = self.resolve(relpath)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        fh = open(path, "wb")
        return fh

    def mkdir(self, relpath):
        os.makedirs(self.resolve(relpath), exist_ok=True)

    def delete(self, relpath):
        path = self.resolve(relpath)
        if path == self.root:
            raise JailError("refusing to delete the file root")
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        elif os.path.exists(path) or os.path.islink(path):
            os.remove(path)
        else:
            raise JailError("no such file: %s" % relpath)

    def rename(self, src, dst):
        s, d = self.resolve(src), self.resolve(dst)
        if s == self.root or d == self.root:
            raise JailError("refusing to move the file root")
        os.rename(s, d)
