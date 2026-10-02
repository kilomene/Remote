#!/usr/bin/env python3
"""yourremote-update: check for and install Remote host updates.

    yourremote-update --check     print latest release vs installed version
    yourremote-update --apply     download + `dpkg -i` the latest .deb (root)

Installed as /usr/bin/yourremote-update.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import updater


def print_check(result):
    print("Installed:  %s" % result["current"])
    print("Latest:     %s  (%s)" % (result["latest"], result.get("release_name") or ""))
    if result.get("published_at"):
        print("Published:  %s" % result["published_at"])
    if result["update_available"]:
        print("Update available: YES")
        for a in result["assets"]:
            size = a.get("size")
            print("  - %s%s" % (a["name"],
                                " (%d bytes)" % size if size else ""))
            print("    %s" % a["url"])
    else:
        print("Update available: no")


def pick_deb_asset(result):
    for a in result["assets"]:
        if updater.ASSET_RE.match(a["name"]):
            return a
    return None


def main():
    ap = argparse.ArgumentParser(description="check for / install Remote updates")
    ap.add_argument("--check", action="store_true", help="print update status")
    ap.add_argument("--apply", action="store_true",
                    help="download and install the latest .deb (needs root)")
    ap.add_argument("--current", default=None,
                    help="override the detected installed version")
    args = ap.parse_args()
    if not (args.check or args.apply):
        args.check = True

    try:
        result = updater.check_update(current=args.current)
    except updater.UpdaterError as e:
        print("ERROR: %s" % e, file=sys.stderr)
        return 1

    if args.check and not args.apply:
        print_check(result)
        return 0

    # --apply
    if not result["update_available"]:
        print("Already up to date (%s)." % result["current"])
        return 0
    asset = pick_deb_asset(result)
    if asset is None:
        print("ERROR: latest release has no remote_*_all.deb asset", file=sys.stderr)
        return 1
    print("Installing %s ..." % asset["name"])
    try:
        out = updater.download_and_install(asset["url"],
                                           expected_size=asset.get("size"),
                                           asset_name=asset["name"])
    except updater.UpdaterError as e:
        print("ERROR: %s" % e, file=sys.stderr)
        return 1
    print(out)
    print("Updated to %s." % result["latest"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
