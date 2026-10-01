#!/usr/bin/env bash
# Build the Remote Android viewer APK without Gradle:
#   aapt2 -> javac -> d8 -> zipalign -> apksigner
#
# Env overrides (for CI):
#   REMOTE_JAVA_HOME  (default: $HOME/workspace/jdk/jdk-17.0.20.1+1)
#   REMOTE_ANDROID_SDK (default: $HOME/workspace/android-sdk)
#   REMOTE_APK_OUT    (default: <repo>/out/remote-viewer.apk)
#   REMOTE_KEYSTORE   (default: $HOME/.remote-viewer.keystore; generated if missing)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

JAVA_HOME="${REMOTE_JAVA_HOME:-$HOME/workspace/jdk/jdk-17.0.20.1+1}"
SDK="${REMOTE_ANDROID_SDK:-$HOME/workspace/android-sdk}"
BT="$SDK/build-tools/34.0.0"
PLATFORM="$SDK/platforms/android-34/android.jar"
OUT="${REMOTE_APK_OUT:-$ROOT/out/remote-viewer.apk}"
KS="${REMOTE_KEYSTORE:-$HOME/.remote-viewer.keystore}"
SRC="$ROOT/android/app/src/main"
B="$(mktemp -d)"

export JAVA_HOME
export PATH="$JAVA_HOME/bin:$PATH"

for f in "$BT/aapt2" "$BT/d8" "$BT/zipalign" "$BT/apksigner" "$PLATFORM"; do
    [ -e "$f" ] || { echo "ERROR: missing $f" >&2; exit 1; }
done

mkdir -p "$B/gen" "$B/classes" "$B/dex" "$(dirname "$OUT")"

echo "[1/6] aapt2 compile+link"
"$BT/aapt2" compile --dir "$SRC/res" -o "$B/res.zip"
"$BT/aapt2" link -o "$B/base.apk" -I "$PLATFORM" \
    --manifest "$SRC/AndroidManifest.xml" --java "$B/gen" "$B/res.zip" \
    --min-sdk-version 26 --target-sdk-version 34

echo "[2/6] javac"
# shellcheck disable=SC2046
javac -encoding UTF-8 -source 8 -target 8 -nowarn -cp "$PLATFORM" -d "$B/classes" \
    $(find "$SRC/java" "$B/gen" -name "*.java")
if [ -z "$(find "$B/classes" -name 'MainActivity.class' 2>/dev/null)" ]; then
    echo "JAVAC FAILED: no MainActivity class produced" >&2; exit 1
fi

echo "[3/6] d8"
# shellcheck disable=SC2046
"$BT/d8" --lib "$PLATFORM" --min-api 26 --output "$B/dex" \
    $(find "$B/classes" -name "*.class")

echo "[4/6] package dex"
cp "$B/base.apk" "$B/unsigned.apk"
(cd "$B/dex" && zip -q -j "$B/unsigned.apk" classes.dex)

echo "[5/6] zipalign"
"$BT/zipalign" -f 4 "$B/unsigned.apk" "$B/aligned.apk"

echo "[6/6] apksigner"
if [ ! -f "$KS" ]; then
    keytool -genkeypair -keystore "$KS" -storepass remoteviewer -keypass remoteviewer \
        -alias remoteviewer -keyalg RSA -keysize 2048 -validity 10950 \
        -dname "CN=Remote Viewer" >/dev/null 2>&1
    chmod 600 "$KS"
fi
"$BT/apksigner" sign --ks "$KS" --ks-pass pass:remoteviewer --key-pass pass:remoteviewer \
    --out "$OUT" "$B/aligned.apk"

"$BT/apksigner" verify --print-certs "$OUT" | head -3
rm -rf "$B"
ls -la "$OUT"
echo "APK built: $OUT"
