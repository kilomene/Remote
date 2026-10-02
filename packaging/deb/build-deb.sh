#!/usr/bin/env bash
# Build the `remote` .deb with dpkg-deb. Pure-python package (arch: all).
# Vendored wheels (mss, pynput) live in packaging/vendor/ and are unpacked
# into /opt/remote/vendor inside the package.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
VERSION="$(cat "$ROOT/version.txt" | tr -d '[:space:]')"
STAGE="$(mktemp -d)"
OUT="$ROOT/out"
DEB="$OUT/remote_${VERSION}_all.deb"

mkdir -p "$OUT" \
  "$STAGE/DEBIAN" \
  "$STAGE/opt/remote/lib" \
  "$STAGE/opt/remote/vendor" \
  "$STAGE/usr/bin" \
  "$STAGE/lib/systemd/system"

# control file with version substituted.
# NOTE: control.in lists tailscale under Recommends, NOT Depends -- tailscale
# is not in Ubuntu main, and the first-run wizard (yourremote-network-setup)
# installs it from Tailscale's official apt repo when missing.
sed "s/@VERSION@/$VERSION/" "$ROOT/packaging/deb/DEBIAN/control.in" > "$STAGE/DEBIAN/control"
cp "$ROOT/packaging/deb/DEBIAN/postinst" "$ROOT/packaging/deb/DEBIAN/prerm" "$STAGE/DEBIAN/"
chmod 755 "$STAGE/DEBIAN"
chmod 755 "$STAGE/DEBIAN/postinst" "$STAGE/DEBIAN/prerm"

# python sources (flat layout; remote_proto.py sits next to the scripts).
# host/*.py is globbed so new host modules ship without editing this list.
# (__pycache__ is a directory: the *.py glob never matches it.)
cp "$ROOT"/host/*.py \
   "$ROOT/viewer/remote_viewer.py" "$ROOT/common/remote_proto.py" \
   "$STAGE/opt/remote/lib/"
chmod 755 "$STAGE/opt/remote/lib/"*.py

# installed version marker, read by host/updater.py (installed_version)
cp "$ROOT/version.txt" "$STAGE/opt/remote/version.txt"
chmod 644 "$STAGE/opt/remote/version.txt"

# vendored pure-python wheels -> /opt/remote/vendor
shopt -s nullglob
WHEELS=("$ROOT"/packaging/vendor/*.whl)
if [ ${#WHEELS[@]} -eq 0 ]; then
    echo "ERROR: no wheels in packaging/vendor/ (expected mss + pynput)" >&2
    exit 1
fi
for whl in "${WHEELS[@]}"; do
    echo "vendoring $(basename "$whl")"
    python3 -m zipfile -e "$whl" "$STAGE/opt/remote/vendor/"
done
# drop dist-info noise to keep the package lean
rm -rf "$STAGE/opt/remote/vendor/"*.dist-info

# /usr/bin symlinks
ln -s /opt/remote/lib/remote_host.py "$STAGE/usr/bin/remote-host"
ln -s /opt/remote/lib/remote_viewer.py "$STAGE/usr/bin/remote-viewer"
ln -s /opt/remote/lib/remote_set_password.py "$STAGE/usr/bin/remote-set-password"
ln -s /opt/remote/lib/yourremote_settings.py "$STAGE/usr/bin/yourremote-settings"
ln -s /opt/remote/lib/yourremote_network_setup.py "$STAGE/usr/bin/yourremote-network-setup"
ln -s /opt/remote/lib/yourremote_update.py "$STAGE/usr/bin/yourremote-update"
ln -s /opt/remote/lib/yourremote_pair_code.py "$STAGE/usr/bin/yourremote-pair-code"

# example configs -> /usr/share/doc/remote/examples/
mkdir -p "$STAGE/usr/share/doc/remote/examples"
cp "$ROOT"/packaging/deb/examples/*.example "$STAGE/usr/share/doc/remote/examples/"
chmod 644 "$STAGE/usr/share/doc/remote/examples/"*.example

# systemd unit
cp "$ROOT/packaging/systemd/remote-host.service" "$STAGE/lib/systemd/system/"

dpkg-deb --build "$STAGE" "$DEB" >/dev/null
rm -rf "$STAGE"
echo "built $DEB"
dpkg-deb -f "$DEB" Package Version Architecture Depends
