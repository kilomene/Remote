package com.remote.viewer.features.notify;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.content.Context;
import android.os.Build;

import com.remote.viewer.Prefs;
import com.remote.viewer.R;

/**
 * App-lifecycle notifications (no background service): session
 * connected/disconnected, file-transfer completion, and host-offline
 * transitions seen by the device-list probe while the app is open.
 */
public final class Notify {

    private static final String CHANNEL = "remote";
    private static int nextId = 100;

    private Notify() {}

    public static void ensureChannel(Context ctx) {
        if (Build.VERSION.SDK_INT < 26) return;
        NotificationManager nm = (NotificationManager)
                ctx.getSystemService(Context.NOTIFICATION_SERVICE);
        if (nm == null) return;
        if (nm.getNotificationChannel(CHANNEL) != null) return;
        NotificationChannel ch = new NotificationChannel(CHANNEL, "Remote",
                NotificationManager.IMPORTANCE_DEFAULT);
        ch.setDescription("Session, transfer and host status events");
        nm.createNotificationChannel(ch);
    }

    private static void post(Context ctx, String title, String text) {
        postId(ctx, title, text, nextId++);
    }

    private static void postId(Context ctx, String title, String text, int id) {
        ensureChannel(ctx);
        NotificationManager nm = (NotificationManager)
                ctx.getSystemService(Context.NOTIFICATION_SERVICE);
        if (nm == null) return;
        Notification.Builder b;
        if (Build.VERSION.SDK_INT >= 26) {
            b = new Notification.Builder(ctx, CHANNEL);
        } else {
            b = new Notification.Builder(ctx);
        }
        b.setContentTitle(title)
                .setContentText(text)
                .setSmallIcon(R.drawable.ic_logo)
                .setAutoCancel(true);
        nm.notify(id, b.build());
    }

    public static void sessionConnected(Context ctx, String device) {
        if (!Prefs.getBool(ctx, Prefs.K_NOTIF_SESSION, true)) return;
        post(ctx, ctx.getString(R.string.notif_connected), device);
    }

    public static void sessionDisconnected(Context ctx, String device, String reason) {
        if (!Prefs.getBool(ctx, Prefs.K_NOTIF_SESSION, true)) return;
        String text = device;
        if (reason != null && !reason.isEmpty()) text += " — " + reason;
        post(ctx, ctx.getString(R.string.notif_disconnected), text);
    }

    public static void transferDone(Context ctx, String name) {
        if (!Prefs.getBool(ctx, Prefs.K_NOTIF_TRANSFER, true)) return;
        post(ctx, ctx.getString(R.string.notif_transfer_done), name);
    }

    public static void transferFailed(Context ctx, String name, String reason) {
        if (!Prefs.getBool(ctx, Prefs.K_NOTIF_TRANSFER, true)) return;
        post(ctx, ctx.getString(R.string.notif_transfer_failed), name + ": " + reason);
    }

    public static void hostOffline(Context ctx, String device) {
        if (!Prefs.getBool(ctx, Prefs.K_NOTIF_OFFLINE, true)) return;
        postId(ctx, ctx.getString(R.string.notif_host_offline), device,
                makeHostOfflineId(device));
    }

    public static void dismissHostOffline(Context ctx, String device) {
        NotificationManager nm = (NotificationManager)
                ctx.getSystemService(Context.NOTIFICATION_SERVICE);
        if (nm != null) nm.cancel(makeHostOfflineId(device));
    }

    private static int makeHostOfflineId(String device) {
        // stable per device so re-notify updates the same notification
        return 900000 + Math.abs(device.hashCode() % 90000);
    }
}
