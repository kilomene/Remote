package com.remote.viewer;

import android.content.Context;
import android.content.SharedPreferences;

/** Non-sensitive display/session preferences. Passwords live in SecureStore. */
public final class Prefs {
    private static final String FILE = "remote_prefs";

    public final boolean haptics;
    public final boolean keepAwake;
    public final boolean smoothScaling;
    public final boolean clipboard;
    public final int fpsCap;

    // ---- extended setting keys (defaults documented) ------------------------
    public static final String K_QUALITY = "quality";          // ultra|high|balanced|low|minlat
    public static final String K_FPS = "fps_sel";             // 15|30|45|60
    public static final String K_CONTROL_MODE = "control_mode"; // touch|trackpad|mouse
    public static final String K_SCALE_MODE = "scale_mode";   // fit|original
    public static final String K_ADAPTIVE = "adaptive";
    public static final String K_PRECISION = "precision";
    public static final String K_SENSITIVITY = "sensitivity";  // 50..200 %
    public static final String K_FULLSCREEN = "fullscreen_def";
    public static final String K_ORIENTATION = "orientation"; // sensor|landscape|portrait
    public static final String K_AUTORECONNECT = "autoreconnect";
    public static final String K_SESSION_TIMEOUT_MIN = "session_timeout_min"; // 0 = off
    public static final String K_BIOMETRIC = "biometric_lock";
    public static final String K_NOTIF_SESSION = "notif_session";
    public static final String K_NOTIF_TRANSFER = "notif_transfer";
    public static final String K_NOTIF_OFFLINE = "notif_offline";
    public static final String K_TERM_FONT = "term_font_sp";
    public static final String K_SHORTCUTS = "shortcut_bar";  // JSON array of key names
    public static final String K_CLIP_HISTORY = "clip_history"; // JSON array, last 20
    public static final String K_HOST_META = "host_meta";     // JSON {host:port: {...}}

    private Prefs(boolean haptics, boolean keepAwake, boolean smoothScaling,
                  boolean clipboard, int fpsCap) {
        this.haptics = haptics;
        this.keepAwake = keepAwake;
        this.smoothScaling = smoothScaling;
        this.clipboard = clipboard;
        this.fpsCap = fpsCap;
    }

    public static Prefs load(Context ctx) {
        SharedPreferences p = ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE);
        return new Prefs(
                p.getBoolean("haptics", true),
                p.getBoolean("keep_awake", true),
                p.getBoolean("smooth", true),
                p.getBoolean("clipboard", true),
                p.getInt("fps_cap", 60));
    }

    public static void save(Context ctx, boolean haptics, boolean keepAwake,
                            boolean smooth, boolean clipboard, int fpsCap) {
        ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE).edit()
                .putBoolean("haptics", haptics)
                .putBoolean("keep_awake", keepAwake)
                .putBoolean("smooth", smooth)
                .putBoolean("clipboard", clipboard)
                .putInt("fps_cap", fpsCap)
                .apply();
    }

    private Prefs() { this(true, true, true, true, 60); }

    // ---- generic typed helpers for the extended settings --------------------

    public static String getString(Context ctx, String key, String def) {
        return ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE).getString(key, def);
    }

    public static void putString(Context ctx, String key, String val) {
        ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE).edit()
                .putString(key, val).apply();
    }

    public static int getInt(Context ctx, String key, int def) {
        return ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE).getInt(key, def);
    }

    public static void putInt(Context ctx, String key, int val) {
        ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE).edit()
                .putInt(key, val).apply();
    }

    public static boolean getBool(Context ctx, String key, boolean def) {
        return ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE).getBoolean(key, def);
    }

    public static void putBool(Context ctx, String key, boolean val) {
        ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE).edit()
                .putBoolean(key, val).apply();
    }

    public static long getLong(Context ctx, String key, long def) {
        return ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE).getLong(key, def);
    }

    public static void putLong(Context ctx, String key, long val) {
        ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE).edit()
                .putLong(key, val).apply();
    }
}
