package com.remote.viewer.features.clipboard;

import android.content.Context;

import com.remote.viewer.Prefs;

import org.json.JSONArray;

import java.util.ArrayList;
import java.util.List;

/**
 * In-app clipboard history: the last 20 clipboard texts seen in either
 * direction, persisted in Prefs as a JSON array. Tapping an entry copies it
 * back to the local clipboard (handled by the caller).
 */
public final class ClipboardHistory {

    private static final int MAX = 20;

    private ClipboardHistory() {}

    public static synchronized void add(Context ctx, String text) {
        if (text == null || text.isEmpty()) return;
        List<String> items = list(ctx);
        items.remove(text); // most-recent-first, no dupes
        items.add(0, text);
        while (items.size() > MAX) items.remove(items.size() - 1);
        Prefs.putString(ctx, Prefs.K_CLIP_HISTORY, new JSONArray(items).toString());
    }

    public static synchronized List<String> list(Context ctx) {
        List<String> out = new ArrayList<String>();
        try {
            JSONArray arr = new JSONArray(
                    Prefs.getString(ctx, Prefs.K_CLIP_HISTORY, "[]"));
            for (int i = 0; i < arr.length(); i++) {
                String s = arr.optString(i, null);
                if (s != null) out.add(s);
            }
        } catch (Exception ignored) {
        }
        return out;
    }

    public static synchronized void clear(Context ctx) {
        Prefs.putString(ctx, Prefs.K_CLIP_HISTORY, "[]");
    }
}
