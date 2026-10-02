#!/usr/bin/env bash
# Build the Remote Android viewer APK without Gradle:
#   aapt2 -> javac -> d8 -> zipalign -> apksigner
#
# Env overrides (for CI):
#   REMOTE_JAVA_HOME    explicit JDK dir (default: $JAVA_HOME, else javac on PATH)
#   REMOTE_ANDROID_SDK  explicit SDK dir (default: auto-detected)
#   REMOTE_BUILD_TOOLS  explicit build-tools version, e.g. 34.0.0 (default: newest installed)
#   REMOTE_PLATFORM     explicit platform version, e.g. 34 (default: newest installed)
#   REMOTE_APK_OUT      (default: <repo>/out/remote-viewer.apk)
#   REMOTE_KEYSTORE     (default: $HOME/.remote-viewer.keystore; generated if missing)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# --- toolchain detection -------------------------------------------------
# An SDK dir counts if it has platforms/ (tools are picked per-version below).
detect_sdk() {
    if [ -n "${REMOTE_ANDROID_SDK:-}" ] && [ -d "${REMOTE_ANDROID_SDK}/platforms" ]; then
        printf '%s\n' "$REMOTE_ANDROID_SDK"; return 0
    fi
    local v
    for v in "${ANDROID_HOME:-}" "${ANDROID_SDK_ROOT:-}" \
             /usr/lib/android-sdk /opt/android-sdk \
             "$HOME/Android/Sdk" "$HOME/workspace/android-sdk"; do
        if [ -n "$v" ] && [ -d "$v/platforms" ]; then
            printf '%s\n' "$v"; return 0
        fi
    done
    return 1
}

# Newest build-tools dir containing all four required tools.
detect_build_tools() {
    local sdk="$1" best="" v d t ok
    if [ -n "${REMOTE_BUILD_TOOLS:-}" ]; then
        d="$sdk/build-tools/$REMOTE_BUILD_TOOLS"
        [ -d "$d" ] && { printf '%s\n' "$d"; return 0; }
        echo "ERROR: REMOTE_BUILD_TOOLS=$REMOTE_BUILD_TOOLS not found under $sdk/build-tools" >&2
        return 1
    fi
    for d in "$sdk"/build-tools/*/; do
        [ -d "$d" ] || continue
        ok=1
        for t in aapt2 d8 zipalign apksigner; do
            [ -x "$d$t" ] || { ok=0; break; }
        done
        [ "$ok" = 1 ] || continue
        v="$(basename "$d")"
        if [ -z "$best" ] || [ "$(printf '%s\n%s\n' "$best" "$v" | sort -V | tail -n 1)" = "$v" ]; then
            best="$v"
        fi
    done
    [ -n "$best" ] || return 1
    printf '%s\n' "$sdk/build-tools/$best"
}

# Newest android-N platform jar.
detect_platform() {
    local sdk="$1" best="" best_n=0 d bn n
    if [ -n "${REMOTE_PLATFORM:-}" ]; then
        d="$sdk/platforms/android-$REMOTE_PLATFORM/android.jar"
        [ -f "$d" ] && { printf '%s\n' "$d"; return 0; }
        echo "ERROR: REMOTE_PLATFORM=$REMOTE_PLATFORM not found under $sdk/platforms" >&2
        return 1
    fi
    for d in "$sdk"/platforms/android-*/; do
        [ -f "${d}android.jar" ] || continue
        bn="$(basename "$d")"; n="${bn#android-}"
        case "$n" in ''|*[!0-9]*) continue;; esac
        if [ "$n" -gt "$best_n" ]; then best="${d}android.jar"; best_n="$n"; fi
    done
    [ -n "$best" ] || return 1
    printf '%s\n' "$best"
}

detect_java_home() {
    if [ -n "${REMOTE_JAVA_HOME:-}" ] && [ -x "${REMOTE_JAVA_HOME}/bin/javac" ]; then
        printf '%s\n' "$REMOTE_JAVA_HOME"; return 0
    fi
    if [ -n "${JAVA_HOME:-}" ] && [ -x "${JAVA_HOME}/bin/javac" ]; then
        printf '%s\n' "$JAVA_HOME"; return 0
    fi
    # sandbox-local JDK install (kept for local builds)
    if [ -x "$HOME/workspace/jdk/jdk-17.0.20.1+1/bin/javac" ]; then
        printf '%s\n' "$HOME/workspace/jdk/jdk-17.0.20.1+1"; return 0
    fi
    local jc
    jc="$(command -v javac || true)"
    if [ -n "$jc" ]; then
        printf '%s\n' "$(dirname "$(dirname "$(readlink -f "$jc")")")"; return 0
    fi
    return 1
}
# --- end detection ---------------------------------------------------------

main() {
    local SDK BT PLATFORM JH
    SDK="$(detect_sdk)" || { echo "ERROR: no Android SDK found (set REMOTE_ANDROID_SDK)" >&2; exit 1; }
    BT="$(detect_build_tools "$SDK")" || { echo "ERROR: no usable build-tools in $SDK/build-tools" >&2; exit 1; }
    PLATFORM="$(detect_platform "$SDK")" || { echo "ERROR: no android platform in $SDK/platforms" >&2; exit 1; }
    JH="$(detect_java_home)" || { echo "ERROR: no JDK found (set REMOTE_JAVA_HOME)" >&2; exit 1; }

    local OUT="${REMOTE_APK_OUT:-$ROOT/out/remote-viewer.apk}"
    local KS="${REMOTE_KEYSTORE:-$HOME/.remote-viewer.keystore}"
    local SRC="$ROOT/android/app/src/main"
    local B
    B="$(mktemp -d)"

    export JAVA_HOME="$JH"
    export PATH="$JAVA_HOME/bin:$PATH"

    echo "SDK: $SDK"
    echo "build-tools: $BT"
    echo "platform: $PLATFORM"
    echo "javac: $(command -v javac)"

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
}

# Allow sourcing for unit-testing the detection functions.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
