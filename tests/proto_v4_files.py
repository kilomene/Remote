#!/usr/bin/env python3
"""Protocol v4 files+clipboard checks for remote-host.

Covers host/files.py (multi-root jail, real os.access checks, walk_tree,
symlink-escape rejection) and host/clipboard.py (memory backend text +
image/png, echo guard). Exit 0 and print "V4-FILES CHECKS PASSED".
"""
import base64
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "host"))
sys.path.insert(0, os.path.join(HERE, "..", "common"))

from files import FileTransfer, JailError, CHUNK
from clipboard import ClipboardSync, CLIPBOARD_SET, MAX_IMAGE_BYTES
import remote_proto as proto

PASS = []

# 1x1 transparent PNG (real PNG bytes)
PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c626001000000ffff03000006000557bfabd4"
    "0000000049454e44ae426082")
PNG_1X1_B = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
    "0000000c49444154789c626001000000ffff0300000600050000000000"
    "0000000049454e44ae426082")  # distinct bytes for "external" image


def check(name, cond):
    if not cond:
        print("FAIL: %s" % name)
        sys.exit(1)
    PASS.append(name)


def expect(exc_types, name, fn, *a, **k):
    try:
        fn(*a, **k)
    except exc_types:
        PASS.append(name)
        return
    except Exception as e:  # noqa: BLE001 - report honestly
        print("FAIL: %s (wrong exception: %r)" % (name, e))
        sys.exit(1)
    print("FAIL: %s (no exception raised)" % name)
    sys.exit(1)


def main():
    check("CHUNK is 256KiB", CHUNK == 256 * 1024)
    check("clipboard CLIPBOARD_SET matches proto",
          CLIPBOARD_SET == proto.CLIPBOARD_SET == 0x40)

    tree = tempfile.mkdtemp(prefix="v4files-")
    try:
        os.makedirs(os.path.join(tree, "sub", "nested"))
        os.makedirs(os.path.join(tree, "emptydir"))
        with open(os.path.join(tree, "sub", "file.txt"), "w") as f:
            f.write("hello files")
        with open(os.path.join(tree, "sub", "nested", "deep.txt"), "w") as f:
            f.write("deep")
        with open(os.path.join(tree, "top.bin"), "wb") as f:
            f.write(b"\x00\x01\x02" * 1000)
        noperm = os.path.join(tree, "noperm.txt")
        with open(noperm, "w") as f:
            f.write("secret")
        os.chmod(noperm, 0o000)
        # symlink escaping the jail
        os.symlink("/etc/hostname", os.path.join(tree, "outside_link"))

        ft = FileTransfer(tree)
        check("primary root preserved", ft.root == os.path.realpath(tree))

        # -- list_dir ------------------------------------------------
        entries = ft.list_dir(".")
        names = {e["name"]: e for e in entries}
        check("list_dir finds sub dir", names["sub"]["dir"] is True)
        check("list_dir finds top.bin", names["top.bin"]["dir"] is False)
        check("list_dir entry shape",
              set(names["top.bin"]) == {"name", "size", "dir", "mtime"})
        check("list_dir size honest",
              names["top.bin"]["size"] == os.path.getsize(
                  os.path.join(tree, "top.bin")))

        # -- symlink escape ------------------------------------------
        expect(JailError, "open_read rejects escaping symlink",
               ft.open_read, "outside_link")
        expect(JailError, "list_dir rejects escaping symlink",
               ft.list_dir, "outside_link")

        # -- unreadable file: REAL kernel denial ----------------------
        # We are root in this sandbox, and root bypasses permission bits,
        # so the honest check runs as the unprivileged 'nobody' user via
        # seteuid: the OSError must come from the kernel, not from us.
        euid = os.geteuid()
        os.chmod(tree, 0o755)
        for dp, _, _ in os.walk(tree):
            os.chmod(dp, 0o755)
        size, fh = ft.open_read("noperm.txt")  # root can; kernel says so
        fh.close()
        check("root reads chmod-000 (kernel semantics)", size == 6)
        os.seteuid(65534)  # nobody
        try:
            expect(OSError, "unreadable file raises OSError (as nobody)",
                   ft.open_read, "noperm.txt")
        finally:
            os.seteuid(euid)

        # -- walk_tree ------------------------------------------------
        man = ft.walk_tree("sub")
        by_path = {e["path"]: e for e in man}
        check("walk_tree flat manifest",
              set(by_path) == {"sub/file.txt", "sub/nested",
                               "sub/nested/deep.txt"})
        check("walk_tree dir flags",
              by_path["sub/nested"]["dir"] is True
              and by_path["sub/file.txt"]["dir"] is False)
        check("walk_tree sizes honest",
              by_path["sub/file.txt"]["size"] == len("hello files"))
        check("walk_tree mtime is int",
              isinstance(by_path["sub/file.txt"]["mtime"], int))
        expect(JailError, "walk_tree rejects non-dir", ft.walk_tree, "top.bin")

        # -- writes ----------------------------------------------------
        fh = ft.begin_write("newdir/new.txt", 5)
        fh.write(b"abcde")
        fh.close()
        size, fh = ft.open_read("newdir/new.txt")
        check("begin_write inside jail round-trips",
              size == 5 and fh.read() == b"abcde")
        fh.close()
        expect(JailError, "begin_write outside jail -> JailError",
               ft.begin_write, "../../evil.txt", 3)
        check("no escape file created", not os.path.exists("/evil.txt"))
        ft.mkdir("emptydir2/deep")
        check("mkdir nested", os.path.isdir(
            os.path.join(tree, "emptydir2", "deep")))
        ft.rename("top.bin", "renamed.bin")
        check("rename moves file",
              os.path.exists(os.path.join(tree, "renamed.bin")))
        ft.delete("renamed.bin")
        check("delete file", not os.path.exists(
            os.path.join(tree, "renamed.bin")))
        expect(JailError, "delete refuses file root", ft.delete, ".")

        # -- multi-root jail -------------------------------------------
        parent = tempfile.mkdtemp(prefix="v4roots-")
        try:
            ra = os.path.join(parent, "a")
            rb = os.path.join(parent, "b")
            os.makedirs(ra)
            os.makedirs(rb)
            with open(os.path.join(rb, "shared.txt"), "w") as f:
                f.write("from-b")
            ft2 = FileTransfer(ra, extra_roots=[rb])
            check("extra roots recorded",
                  ft2.roots == [os.path.realpath(ra), os.path.realpath(rb)])
            size, fh = ft2.open_read("../b/shared.txt")
            check("extra root reachable via union jail",
                  size == 6 and fh.read() == b"from-b")
            fh.close()
            expect(JailError, "union jail still rejects true escape",
                   ft2.open_read, "../../etc/hostname")
        finally:
            shutil.rmtree(parent, ignore_errors=True)

        # -- clipboard memory backend -----------------------------------
        sent = []
        cb = ClipboardSync(sent.append, memory=True)
        check("clipboard memory backend enabled", cb.enabled)
        cb.set("hello clipboard")
        check("clipboard set/get text", cb.get() == "hello clipboard")
        check("echo guard: set text not rebroadcast",
              cb._check_once() is None and sent == [])
        # external text change -> broadcast payload
        cb._mem = ("text", "typed locally")
        payload = cb._check_once()
        check("monitor detects external text change",
              payload == {"text": "typed locally"})
        check("no repeat broadcast on second poll", cb._check_once() is None)
        wire = json.dumps(payload).encode()
        check("text wire shape", json.loads(wire) == {"text": "typed locally"})

        # images
        cb.set_image(PNG_1X1)
        check("clipboard set_image/get_image round-trip",
              cb.get_image() == PNG_1X1)
        check("echo guard: set image not rebroadcast",
              cb._check_once() is None)
        cb._mem = ("image", PNG_1X1_B)
        payload = cb._check_once()
        check("monitor detects external image change",
              payload is not None and payload.get("mime") == "image/png")
        check("image wire shape decodes to identical bytes",
              base64.b64decode(payload["data"]) == PNG_1X1_B)
        check("image wire JSON-serializable",
              json.loads(json.dumps(payload))["mime"] == "image/png")
        check("oversize image guard constant sane",
              MAX_IMAGE_BYTES == 2 * 1024 * 1024)

        # wire dispatch
        cb.set_from_wire({"text": "via wire"})
        check("set_from_wire text", cb.get() == "via wire")
        cb.set_from_wire({"mime": "image/png",
                          "data": base64.b64encode(PNG_1X1).decode("ascii")})
        check("set_from_wire image", cb.get_image() == PNG_1X1)
        pl = cb.current_payload()
        check("current_payload image shape",
              pl["mime"] == "image/png"
              and base64.b64decode(pl["data"]) == PNG_1X1)
    finally:
        os.chmod(tree, 0o755)
        shutil.rmtree(tree, ignore_errors=True)

    print("V4-FILES CHECKS PASSED (%d checks)" % len(PASS))


if __name__ == "__main__":
    main()
