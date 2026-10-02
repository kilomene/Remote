#!/usr/bin/env python3
"""File transfer backend for remote-host (protocol v2/v3/v4, 0x50-0x5A).

Wire messages (FILE_*) are unchanged. All paths are jailed under the
configured roots (the user's home by default, plus optional extra roots
from config). Any escape attempt (``..``, absolute-path tricks, symlink
races) raises JailError and is reported to the client as FILE_ERROR.

Multi-root jail
---------------
``FileTransfer(root, extra_roots=[...])``. The jail is the UNION of all
configured roots: a client path is resolved against each root in order
(primary first) and accepted if its realpath lands inside any one of
them. Example with root=``/home/osh`` and extra_roots=[``/mnt/data``]:
the client path ``../../mnt/data/movies`` resolves inside ``/mnt/data``
and is allowed. Single-root deployments behave exactly as before.

Every operation also performs a real ``os.access()`` permission check
(``R_OK`` on reads, ``W_OK`` on the parent directory for writes) in
addition to the realpath jail, so permission failures surface as honest
``OSError``/``PermissionError`` from the kernel, not synthesized errors.

Recursive folder transfers (no new wire messages)
--------------------------------------------------
Folder DOWNLOAD (client algorithm):

1. ``FILE_LIST`` the remote folder; for every entry with ``dir=True``,
   recurse with ``FILE_LIST`` on the joined subpath.
2. For every file entry, issue ``FILE_GET`` + ``FILE_DATA`` chunks +
   ``FILE_DONE`` and recreate the directory structure locally.
3. Empty directories are recreated from the ``dir=True`` entries.
4. To reduce round trips the host can serve a flat manifest from
   :meth:`FileTransfer.walk_tree` (same entry shape as ``FILE_LIST``,
   paths in client namespace so they feed straight into ``FILE_GET``);
   the entries are identical to what recursive ``FILE_LIST`` yields.

Folder UPLOAD (client algorithm):

1. Walk the local tree top-down; for every directory send
   ``FILE_MKDIR`` for the remote path (parents first).
2. For every file send ``FILE_PUT`` {path, size}, then the ``FILE_DATA``
   chunks, then ``FILE_DONE``.
3. The server creates parents as needed (``begin_write``), so a client
   MAY skip ``FILE_MKDIR`` for non-empty dirs, but explicit ``FILE_MKDIR``
   is required for empty directories.

Symlinks: entries whose realpath escapes every configured root are
listed (lstat data, ``dir=False``) but ``open_read``/``open_write`` on
them raises ``JailError``. ``walk_tree`` never follows directory
symlinks (``os.walk`` with ``followlinks=False``).
"""
import errno
import os
import shutil

CHUNK = 256 * 1024  # FILE_DATA chunk size


class JailError(Exception):
    pass


class FileTransfer:
    def __init__(self, root, extra_roots=None):
        roots = [root] + list(extra_roots or [])
        self.roots = []
        for r in roots:
            rp = os.path.realpath(os.path.expanduser(r))
            os.makedirs(rp, exist_ok=True)
            if rp not in self.roots:
                self.roots.append(rp)
        self.root = self.roots[0]  # primary root (API compatibility)

    # -- path resolution -------------------------------------------------

    def _check_access(self, abs_path, mode):
        """Real kernel permission check; raises PermissionError (OSError)."""
        if not os.access(abs_path, mode):
            raise PermissionError(errno.EACCES, "permission denied", abs_path)

    def resolve(self, relpath, want=None):
        """Resolve a client-supplied path inside the union jail.

        ``want`` is an optional os.access() mode (os.R_OK / os.W_OK)
        enforced against the resolved path. Raises JailError on escape,
        PermissionError when the real access check fails.
        """
        if not isinstance(relpath, str) or not relpath:
            raise JailError("empty path")
        # strip leading slashes: everything is relative to a jail root
        rel = relpath.lstrip("/")
        for root in self.roots:
            abs_p = os.path.realpath(os.path.join(root, rel))
            if abs_p == root or abs_p.startswith(root + os.sep):
                if want is not None:
                    self._check_access(abs_p, want)
                return abs_p
        raise JailError("path escapes configured file roots")

    def _require_writable_dir(self, abs_path):
        """The parent dir of abs_path must really be writable (for creates/
        deletes/renames). Uses the nearest existing ancestor."""
        d = abs_path if os.path.isdir(abs_path) else os.path.dirname(abs_path)
        while d and not os.path.exists(d):
            d = os.path.dirname(d)
        if not d:
            raise JailError("no writable parent found")
        self._check_access(d, os.W_OK | os.X_OK)

    # -- reads -----------------------------------------------------------

    def list_dir(self, relpath):
        """Return entries [{name, size, dir, mtime}] for a directory."""
        path = self.resolve(relpath or ".", want=os.R_OK)
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

    def walk_tree(self, relpath):
        """Flat manifest of a directory tree for efficient folder download.

        Returns [{path, size, dir, mtime}] with ``path`` in client
        namespace (relative to the file roots, feedable straight into
        FILE_GET / FILE_MKDIR). Directories are included (dir=True);
        directory symlinks are NOT followed. Entries whose realpath
        escapes the jail are included as lstat entries but cannot be
        opened (open_read raises JailError on them).
        """
        base = self.resolve(relpath or ".", want=os.R_OK)
        if not os.path.isdir(base):
            raise JailError("not a directory: %s" % relpath)
        prefix = (relpath or ".").strip("/")
        if prefix == ".":
            prefix = ""
        manifest = []
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            rel_dir = os.path.relpath(dirpath, base)
            client_dir = prefix if rel_dir == "." else \
                (prefix + "/" + rel_dir if prefix else rel_dir)
            # prune dir symlinks explicitly (belt and braces)
            for dn in list(dirnames):
                full = os.path.join(dirpath, dn)
                if os.path.islink(full):
                    dirnames.remove(dn)
                    try:
                        st = os.lstat(full)
                        manifest.append({
                            "path": client_dir + "/" + dn,
                            "size": st.st_size, "dir": False,
                            "mtime": int(st.st_mtime),
                        })
                    except OSError:
                        pass
            for dn in dirnames:
                try:
                    st = os.lstat(os.path.join(dirpath, dn))
                    manifest.append({
                        "path": client_dir + "/" + dn,
                        "size": st.st_size, "dir": True,
                        "mtime": int(st.st_mtime),
                    })
                except OSError:
                    continue
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                try:
                    st = os.lstat(full)
                    manifest.append({
                        "path": client_dir + "/" + fn,
                        "size": st.st_size,
                        "dir": False,
                        "mtime": int(st.st_mtime),
                    })
                except OSError:
                    continue
        manifest.sort(key=lambda e: e["path"])
        return manifest

    def open_read(self, relpath, offset=0):
        """Return (size, binary file object) for a download."""
        path = self.resolve(relpath, want=os.R_OK)
        if not os.path.isfile(path):
            raise JailError("not a file: %s" % relpath)
        size = os.path.getsize(path)
        if offset < 0 or offset > size:
            raise JailError("bad offset %d for size %d" % (offset, size))
        fh = open(path, "rb")  # real kernel open; OSError propagates honestly
        if offset:
            fh.seek(offset)
        return size, fh

    # -- writes ----------------------------------------------------------

    def begin_write(self, relpath, size):
        """Open a destination for upload; parents are created as needed."""
        path = self.resolve(relpath)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._require_writable_dir(path)
        fh = open(path, "wb")
        return fh

    def mkdir(self, relpath):
        path = self.resolve(relpath)
        self._require_writable_dir(path)
        os.makedirs(path, exist_ok=True)

    def delete(self, relpath):
        path = self.resolve(relpath)
        if path in self.roots:
            raise JailError("refusing to delete a file root")
        self._require_writable_dir(path)
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        elif os.path.exists(path) or os.path.islink(path):
            os.remove(path)
        else:
            raise JailError("no such file: %s" % relpath)

    def rename(self, src, dst):
        s, d = self.resolve(src), self.resolve(dst)
        if s in self.roots or d in self.roots:
            raise JailError("refusing to move a file root")
        self._require_writable_dir(s)
        self._require_writable_dir(d)
        os.rename(s, d)
