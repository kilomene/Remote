package com.remote.viewer.features.clipboard;

import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;

/**
 * v2 clipboard sync (see PROTOCOL.md).
 *
 * Watches the local clipboard: when it changes, the new text is handed to the
 * callback so the caller can send CLIPBOARD_SET. Remote text is applied with
 * {@link #applyRemoteText(String)}; an echo guard suppresses the change event
 * that our own set (and re-receipt of text we just sent) so no echo loop can
 * form. Each side must ignore a change that matches the last text it sent or
 * received.
 */
public class ClipboardSync {

    public interface Callback {
        void onLocalClipboardChanged(String text);
    }

    private final Context ctx;
    private final ClipboardManager cm;
    private final Callback callback;

    private boolean enabled = true;
    private boolean started;
    private String lastSent = "";
    private String lastApplied = "";

    private final ClipboardManager.OnPrimaryClipChangedListener listener =
            new ClipboardManager.OnPrimaryClipChangedListener() {
                @Override
                public void onPrimaryClipChanged() {
                    if (!enabled) return;
                    String t = currentText();
                    if (t == null) return;
                    if (t.equals(lastApplied) || t.equals(lastSent)) return;
                    lastSent = t;
                    callback.onLocalClipboardChanged(t);
                }
            };

    public ClipboardSync(Context ctx, Callback callback) {
        this.ctx = ctx.getApplicationContext();
        this.cm = (ClipboardManager) this.ctx.getSystemService(Context.CLIPBOARD_SERVICE);
        this.callback = callback;
    }

    public void setEnabled(boolean enabled) {
        this.enabled = enabled;
    }

    public void start() {
        if (!started && cm != null) {
            cm.addPrimaryClipChangedListener(listener);
            started = true;
        }
    }

    public void stop() {
        if (started && cm != null) {
            cm.removePrimaryClipChangedListener(listener);
            started = false;
        }
    }

    /** Applies text received from the host. The resulting change event is ignored. */
    public void applyRemoteText(String text) {
        if (text == null || cm == null) return;
        lastApplied = text;
        cm.setPrimaryClip(ClipData.newPlainText("remote", text));
    }

    private String currentText() {
        if (cm == null || !cm.hasPrimaryClip()) return null;
        ClipData clip = cm.getPrimaryClip();
        if (clip == null || clip.getItemCount() == 0) return null;
        CharSequence cs = clip.getItemAt(0).coerceToText(ctx);
        return cs == null ? null : cs.toString();
    }
}
