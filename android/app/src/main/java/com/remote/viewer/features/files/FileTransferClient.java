package com.remote.viewer.features.files;

import android.content.ContentResolver;
import android.content.ContentValues;
import android.content.Context;
import android.net.Uri;
import android.os.Build;
import android.os.Environment;
import android.os.Handler;
import android.os.Looper;
import android.provider.MediaStore;

import com.remote.viewer.RemoteClient;
import com.remote.viewer.RemoteProto;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.io.RandomAccessFile;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;

/**
 * v2 file-transfer client (see PROTOCOL.md).
 *
 * Runs its own RemoteClient connection (with DEVICE_HELLO) and correlates
 * 0x50-0x5A responses to the outstanding operation. One listing, one
 * download and one upload may be active at a time, which is all the
 * FileManagerActivity UI needs. All callbacks are delivered on the main
 * thread.
 *
 * Download: FILE_GET {path, offset} -> FILE_META -> FILE_DATA* -> FILE_DONE.
 * pause() stops writing chunks (the host may keep streaming; its chunks are
 * discarded); resume() re-issues FILE_GET with offset = bytes already
 * written. Progress = bytes_received / size.
 * Upload: FILE_PUT -> FILE_DATA* -> FILE_DONE, then the host replies
 * FILE_DONE as the ack.
 */
public class FileTransferClient implements RemoteClient.Listener {

    private static final java.nio.charset.Charset UTF8 = StandardCharsets.UTF_8;
    private static final int CHUNK = 64 * 1024;

    public static class Entry {
        public final String name;
        public final long size;
        public final boolean dir;
        public final long mtime;

        Entry(String name, long size, boolean dir, long mtime) {
            this.name = name;
            this.size = size;
            this.dir = dir;
            this.mtime = mtime;
        }
    }

    public interface ListCallback {
        void onList(String path, List<Entry> entries);
        void onError(String reason);
    }

    public interface ProgressCallback {
        void onProgress(long done, long total);
    }

    public interface DoneCallback {
        void onDone();
        void onError(String reason);
    }

    public interface StatusListener {
        void onConnected();
        void onAuthFailed(String reason);
        void onDisconnected(boolean willRetry, String reason);
    }

    private static class Download {
        String remotePath;
        File localFile;
        ProgressCallback progressCb;
        DoneCallback doneCb;
        RandomAccessFile raf;
        long written;
        long total = -1;
        volatile boolean paused;
        volatile boolean finished;
    }

    private static class Upload {
        File localFile;
        String remotePath;
        ProgressCallback progressCb;
        DoneCallback doneCb;
        volatile boolean awaitingAck;
        volatile boolean cancelled;
    }

    private final RemoteClient client;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private StatusListener statusListener;

    private ListCallback listCb;
    private Download download;
    private Upload upload;
    private DoneCallback ackCb;

    public FileTransferClient(Context ctx, String host, int port, String password) {
        this.client = new RemoteClient(ctx, host, port, password, this);
    }

    public void setStatusListener(StatusListener l) {
        this.statusListener = l;
    }

    public void start() {
        client.start();
    }

    public void stop() {
        Download d = download;
        if (d != null) closeQuietly(d.raf);
        download = null;
        if (upload != null) upload.cancelled = true;
        upload = null;
        listCb = null;
        ackCb = null;
        client.stop();
    }

    // ---- operations --------------------------------------------------------

    public void listDir(String path, ListCallback cb) {
        listCb = cb;
        client.sendFileList(path == null ? "" : path);
    }

    public void download(String remotePath, File localTmp, long offset,
                         ProgressCallback progressCb, DoneCallback doneCb) {
        Download d = new Download();
        d.remotePath = remotePath;
        d.localFile = localTmp;
        d.progressCb = progressCb;
        d.doneCb = doneCb;
        d.written = offset;
        try {
            if (offset == 0 && localTmp.exists()) localTmp.delete();
            d.raf = new RandomAccessFile(localTmp, "rw");
            if (offset > 0) d.raf.seek(offset);
        } catch (Exception e) {
            final String msg = e.getMessage();
            post(new Runnable() {
                @Override public void run() { doneCb.onError(msg == null ? "io error" : msg); }
            });
            return;
        }
        download = d;
        client.sendFileGet(remotePath, offset);
    }

    public void pauseDownload() {
        Download d = download;
        if (d != null) d.paused = true;
    }

    public void resumeDownload() {
        Download d = download;
        if (d == null || d.finished) return;
        d.paused = false;
        client.sendFileGet(d.remotePath, d.written);
    }

    public boolean isDownloadPaused() {
        Download d = download;
        return d != null && d.paused;
    }

    public void upload(File localFile, String remotePath,
                       ProgressCallback progressCb, DoneCallback doneCb) {
        final Upload u = new Upload();
        u.localFile = localFile;
        u.remotePath = remotePath;
        u.progressCb = progressCb;
        u.doneCb = doneCb;
        upload = u;
        new Thread(new Runnable() {
            @Override public void run() {
                try {
                    long size = localFile.length();
                    client.sendFilePut(remotePath, size);
                    FileInputStream fis = new FileInputStream(localFile);
                    try {
                        byte[] buf = new byte[CHUNK];
                        long sent = 0;
                        int r;
                        final ProgressCallback pcb = progressCb;
                        while ((r = fis.read(buf)) > 0) {
                            if (u.cancelled) return;
                            client.sendFileData(buf, 0, r);
                            sent += r;
                            final long s = sent;
                            post(new Runnable() {
                                @Override public void run() { pcb.onProgress(s, size); }
                            });
                        }
                    } finally {
                        try { fis.close(); } catch (Exception ignored) {}
                    }
                    if (u.cancelled) return;
                    u.awaitingAck = true;
                    client.sendFileDone(remotePath, size);
                } catch (final Exception e) {
                    post(new Runnable() {
                        @Override public void run() {
                            upload = null;
                            String m = e.getMessage();
                            doneCb.onError(m == null ? "io error" : m);
                        }
                    });
                }
            }
        }).start();
    }

    public void mkdir(String path, DoneCallback cb) {
        ackCb = cb;
        client.sendFileMkdir(path);
    }

    public void delete(String path, DoneCallback cb) {
        ackCb = cb;
        client.sendFileDelete(path);
    }

    public void rename(String from, String to, DoneCallback cb) {
        ackCb = cb;
        client.sendFileRename(from, to);
    }

    // ---- RemoteClient.Listener ----------------------------------------------

    @Override public void onConnected() {
        final StatusListener l = statusListener;
        if (l != null) ui.post(new Runnable() {
            @Override public void run() { l.onConnected(); }
        });
    }

    @Override public void onAuthFailed(final String reason) {
        failAll(reason == null ? "auth failed" : reason);
        final StatusListener l = statusListener;
        if (l != null) ui.post(new Runnable() {
            @Override public void run() { l.onAuthFailed(reason); }
        });
    }

    @Override public void onDisconnected(final boolean willRetry, final String reason) {
        if (!willRetry) failAll(reason == null ? "disconnected" : reason);
        final StatusListener l = statusListener;
        if (l != null) ui.post(new Runnable() {
            @Override public void run() { l.onDisconnected(willRetry, reason); }
        });
    }

    @Override public void onFrame(byte[] jpeg) { }
    @Override public void onStats(int fps, long rttMs) { }
    @Override public void onClipboardText(String text) { }

    @Override
    public void onFileMsg(int type, byte[] payload) {
        switch (type) {
            case RemoteProto.FILE_LIST_RESP: handleListResp(payload); break;
            case RemoteProto.FILE_META: handleMeta(payload); break;
            case RemoteProto.FILE_DATA: handleData(payload); break;
            case RemoteProto.FILE_DONE: handleDone(payload); break;
            case RemoteProto.FILE_ERROR: handleError(payload); break;
            default: break;
        }
    }

    private void handleListResp(byte[] payload) {
        final ListCallback cb = listCb;
        if (cb == null) return;
        listCb = null;
        try {
            JSONObject o = new JSONObject(new String(payload, UTF8));
            final String path = o.optString("path", "");
            JSONArray arr = o.optJSONArray("entries");
            final List<Entry> entries = new ArrayList<Entry>();
            if (arr != null) {
                for (int i = 0; i < arr.length(); i++) {
                    JSONObject e = arr.getJSONObject(i);
                    entries.add(new Entry(e.getString("name"), e.optLong("size", 0),
                            e.optBoolean("dir", false), e.optLong("mtime", 0)));
                }
            }
            post(new Runnable() {
                @Override public void run() { cb.onList(path, entries); }
            });
        } catch (final JSONException e) {
            post(new Runnable() {
                @Override public void run() { cb.onError("bad list response"); }
            });
        }
    }

    private void handleMeta(byte[] payload) {
        final Download d = download;
        if (d == null || d.finished) return;
        try {
            long size = new JSONObject(new String(payload, UTF8)).getLong("size");
            d.total = size;
            reportProgress(d);
        } catch (JSONException ignored) {
        }
    }

    private void handleData(byte[] payload) {
        final Download d = download;
        if (d == null || d.finished || d.paused || d.raf == null) return;
        try {
            d.raf.write(payload);
            d.written += payload.length;
            reportProgress(d);
        } catch (Exception e) {
            finishDownloadError(d, "write failed");
        }
    }

    private void handleDone(byte[] payload) {
        // Upload ack takes priority: the host replies FILE_DONE after our upload.
        Upload u = upload;
        if (u != null && u.awaitingAck) {
            upload = null;
            final DoneCallback cb = u.doneCb;
            post(new Runnable() {
                @Override public void run() { cb.onDone(); }
            });
            return;
        }
        DoneCallback ack = ackCb;
        if (ack != null) {
            ackCb = null;
            post(new Runnable() {
                @Override public void run() { ack.onDone(); }
            });
            return;
        }
        Download d = download;
        if (d != null && !d.finished) {
            d.finished = true;
            download = null;
            closeQuietly(d.raf);
            final DoneCallback cb = d.doneCb;
            post(new Runnable() {
                @Override public void run() { cb.onDone(); }
            });
        }
    }

    private void handleError(byte[] payload) {
        String op = "";
        String reason = "error";
        try {
            JSONObject o = new JSONObject(new String(payload, UTF8));
            op = o.optString("op", "");
            reason = o.optString("reason", reason);
        } catch (JSONException ignored) {
        }
        final String r = reason;
        if ("list".equals(op)) {
            final ListCallback cb = listCb;
            listCb = null;
            if (cb != null) post(new Runnable() {
                @Override public void run() { cb.onError(r); }
            });
        } else if ("get".equals(op)) {
            Download d = download;
            if (d != null) finishDownloadError(d, r);
        } else if ("put".equals(op)) {
            final Upload u = upload;
            upload = null;
            if (u != null) post(new Runnable() {
                @Override public void run() { u.doneCb.onError(r); }
            });
        } else {
            final DoneCallback ack = ackCb;
            ackCb = null;
            if (ack != null) post(new Runnable() {
                @Override public void run() { ack.onError(r); }
            });
        }
    }

    private void reportProgress(final Download d) {
        if (d.progressCb == null) return;
        final long w = d.written;
        final long t = d.total;
        post(new Runnable() {
            @Override public void run() { d.progressCb.onProgress(w, t); }
        });
    }

    private void finishDownloadError(Download d, final String reason) {
        d.finished = true;
        download = null;
        closeQuietly(d.raf);
        final DoneCallback cb = d.doneCb;
        post(new Runnable() {
            @Override public void run() { cb.onError(reason); }
        });
    }

    private void failAll(final String reason) {
        final ListCallback lc = listCb; listCb = null;
        final DoneCallback ack = ackCb; ackCb = null;
        Download d = download; download = null;
        if (d != null) { d.finished = true; closeQuietly(d.raf); }
        final DoneCallback dc = d == null ? null : d.doneCb;
        final Upload u = upload; upload = null;
        post(new Runnable() {
            @Override public void run() {
                if (lc != null) lc.onError(reason);
                if (ack != null) ack.onError(reason);
                if (dc != null) dc.onError(reason);
                if (u != null) u.doneCb.onError(reason);
            }
        });
    }

    private void post(Runnable r) {
        ui.post(r);
    }

    private static void closeQuietly(RandomAccessFile raf) {
        if (raf == null) return;
        try { raf.close(); } catch (Exception ignored) {}
    }

    // ---- downloads: publish a finished temp file into the user's Downloads ---

    /**
     * Copies a finished temp download into the user's Downloads collection.
     * API 29+: MediaStore. API 26-28: legacy public Downloads dir (needs
     * WRITE_EXTERNAL_STORAGE, which the manifest declares with
     * maxSdkVersion="28").
     *
     * @return true on success.
     */
    public static boolean installDownload(Context ctx, File tmp, String name) {
        if (tmp == null || !tmp.exists() || name == null || name.isEmpty()) return false;
        try {
            if (Build.VERSION.SDK_INT >= 29) {
                ContentValues v = new ContentValues();
                v.put(MediaStore.Downloads.DISPLAY_NAME, name);
                v.put(MediaStore.Downloads.MIME_TYPE, "application/octet-stream");
                v.put(MediaStore.Downloads.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS);
                ContentResolver cr = ctx.getContentResolver();
                Uri uri = cr.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, v);
                if (uri == null) return false;
                OutputStream os = cr.openOutputStream(uri);
                if (os == null) return false;
                try {
                    copy(new FileInputStream(tmp), os);
                } finally {
                    try { os.close(); } catch (Exception ignored) {}
                }
                return true;
            } else {
                File dir = Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS);
                if (!dir.exists() && !dir.mkdirs()) return false;
                copy(new FileInputStream(tmp), new FileOutputStream(new File(dir, name)));
                return true;
            }
        } catch (Exception e) {
            return false;
        }
    }

    private static void copy(InputStream in, OutputStream os) throws Exception {
        try {
            byte[] buf = new byte[65536];
            int r;
            while ((r = in.read(buf)) > 0) os.write(buf, 0, r);
        } finally {
            try { in.close(); } catch (Exception ignored) {}
            try { os.close(); } catch (Exception ignored) {}
        }
    }
}
