package com.remote.viewer.features.recording;

import android.content.ContentResolver;
import android.content.ContentValues;
import android.content.Context;
import android.content.Intent;
import android.graphics.Bitmap;
import android.net.Uri;
import android.os.Build;
import android.os.Environment;
import android.provider.MediaStore;

import java.io.File;
import java.io.FileOutputStream;
import java.io.OutputStream;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

/**
 * Screenshots: captures the current decoded frame (supplied by the caller),
 * saves it to the gallery (MediaStore on API 29+, legacy Pictures dir
 * below), and offers share via ACTION_SEND chooser.
 */
public final class Screenshots {

    private Screenshots() {}

    public interface Callback {
        void onSaved(Uri uri);
        void onError(String reason);
    }

    /** Compresses the bitmap to PNG and publishes it. Runs on caller thread. */
    public static void capture(Context ctx, Bitmap frame, Callback cb) {
        if (frame == null || frame.isRecycled()) {
            if (cb != null) cb.onError("no frame yet");
            return;
        }
        String name = "remote_"
                + new SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(new Date())
                + ".png";
        try {
            Uri uri;
            if (Build.VERSION.SDK_INT >= 29) {
                ContentValues v = new ContentValues();
                v.put(MediaStore.Images.Media.DISPLAY_NAME, name);
                v.put(MediaStore.Images.Media.MIME_TYPE, "image/png");
                v.put(MediaStore.Images.Media.RELATIVE_PATH,
                        Environment.DIRECTORY_PICTURES + "/Remote");
                ContentResolver cr = ctx.getContentResolver();
                uri = cr.insert(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, v);
                if (uri == null) {
                    if (cb != null) cb.onError("MediaStore insert failed");
                    return;
                }
                OutputStream os = cr.openOutputStream(uri);
                if (os == null) {
                    if (cb != null) cb.onError("could not open output");
                    return;
                }
                try {
                    frame.compress(Bitmap.CompressFormat.PNG, 100, os);
                } finally {
                    try { os.close(); } catch (Exception ignored) {}
                }
            } else {
                File dir = new File(Environment.getExternalStoragePublicDirectory(
                        Environment.DIRECTORY_PICTURES), "Remote");
                if (!dir.exists() && !dir.mkdirs()) {
                    if (cb != null) cb.onError("could not create dir");
                    return;
                }
                File f = new File(dir, name);
                FileOutputStream fos = new FileOutputStream(f);
                try {
                    frame.compress(Bitmap.CompressFormat.PNG, 100, fos);
                } finally {
                    try { fos.close(); } catch (Exception ignored) {}
                }
                uri = Uri.fromFile(f);
            }
            if (cb != null) cb.onSaved(uri);
        } catch (Exception e) {
            if (cb != null) {
                String m = e.getMessage();
                cb.onError(m == null ? "save failed" : m);
            }
        }
    }

    /** Opens the system share chooser for a saved screenshot. */
    public static void share(Context ctx, Uri uri) {
        if (uri == null) return;
        Intent i = new Intent(Intent.ACTION_SEND);
        i.setType("image/png");
        i.putExtra(Intent.EXTRA_STREAM, uri);
        i.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
        ctx.startActivity(Intent.createChooser(i, "Share screenshot"));
    }
}
