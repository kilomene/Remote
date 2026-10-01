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
}
