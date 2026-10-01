package com.remote.viewer;

import android.content.Context;

import java.io.IOException;
import java.io.OutputStream;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;
import java.util.Arrays;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;

import org.json.JSONException;
import org.json.JSONObject;

/**
 * Threaded Remote wire-protocol v2 client.
 * Connects, sends DEVICE_HELLO, authenticates, pumps frames, sends input,
 * measures RTT, relays clipboard and file messages, and auto-reconnects
 * with exponential backoff until stop() is called.
 *
 * v1 behavior is unchanged: without a Context no DEVICE_HELLO is sent and
 * v2 messages are simply never produced.
 */
public class RemoteClient {

    private static final java.nio.charset.Charset UTF8 = StandardCharsets.UTF_8;

    public interface Listener {
        void onConnected();
        void onAuthFailed(String reason);
        void onDisconnected(boolean willRetry, String reason);
        void onFrame(byte[] jpeg);
        void onStats(int fps, long rttMs);
        void onClipboardText(String text);
        void onFileMsg(int type, byte[] payload);
    }

    private final Context ctx;
    private final String host;
    private final int port;
    private final String password;
    private final Listener listener;
    private final ExecutorService net = Executors.newCachedThreadPool();
    private final AtomicBoolean stop = new AtomicBoolean(false);
    private final SecureRandom random = new SecureRandom();

    private volatile OutputStream out;
    private volatile long pingSentAt;
    private volatile byte[] pingToken;

    /** v1-style client: no DEVICE_HELLO is sent. */
    public RemoteClient(String host, int port, String password, Listener listener) {
        this(null, host, port, password, listener);
    }

    /** v2 client: DEVICE_HELLO is sent before auth when ctx is non-null. */
    public RemoteClient(Context ctx, String host, int port, String password, Listener listener) {
        this.ctx = ctx == null ? null : ctx.getApplicationContext();
        this.host = host;
        this.port = port;
        this.password = password;
        this.listener = listener;
    }

    public void start() {
        net.execute(this::runLoop);
    }

    public void stop() {
        stop.set(true);
        net.shutdownNow();
        closeOut();
    }

    // ---- outbound -----------------------------------------------------------

    private void send(int type, byte[] payload) {
        OutputStream o = out;
        if (o == null) return;
        try {
            RemoteProto.sendMsg(o, type, payload);
        } catch (IOException ignored) {
        }
    }

    public void sendInput(String json) {
        send(RemoteProto.INPUT, json.getBytes(UTF8));
    }

    public void sendDisconnect() {
        send(RemoteProto.DISCONNECT, new byte[0]);
    }

    public void sendClipboard(String text) {
        try {
            JSONObject o = new JSONObject();
            o.put("text", text == null ? "" : text);
            send(RemoteProto.CLIPBOARD_SET, o.toString().getBytes(UTF8));
        } catch (JSONException ignored) {
        }
    }

    public void sendFileList(String path) {
        send(RemoteProto.FILE_LIST, jsonPath(path));
    }

    public void sendFileGet(String path, long offset) {
        try {
            JSONObject o = new JSONObject();
            o.put("path", path);
            o.put("offset", offset);
            send(RemoteProto.FILE_GET, o.toString().getBytes(UTF8));
        } catch (JSONException ignored) {
        }
    }

    public void sendFilePut(String path, long size) {
        try {
            JSONObject o = new JSONObject();
            o.put("path", path);
            o.put("size", size);
            send(RemoteProto.FILE_PUT, o.toString().getBytes(UTF8));
        } catch (JSONException ignored) {
        }
    }

    public void sendFileData(byte[] buf, int off, int len) {
        send(RemoteProto.FILE_DATA, Arrays.copyOfRange(buf, off, off + len));
    }

    public void sendFileDone(String path, long size) {
        try {
            JSONObject o = new JSONObject();
            o.put("path", path);
            o.put("size", size);
            send(RemoteProto.FILE_DONE, o.toString().getBytes(UTF8));
        } catch (JSONException ignored) {
        }
    }

    public void sendFileMkdir(String path) {
        send(RemoteProto.FILE_MKDIR, jsonPath(path));
    }

    public void sendFileDelete(String path) {
        send(RemoteProto.FILE_DELETE, jsonPath(path));
    }

    public void sendFileRename(String from, String to) {
        try {
            JSONObject o = new JSONObject();
            o.put("from", from);
            o.put("to", to);
            send(RemoteProto.FILE_RENAME, o.toString().getBytes(UTF8));
        } catch (JSONException ignored) {
        }
    }

    private static byte[] jsonPath(String path) {
        try {
            JSONObject o = new JSONObject();
            o.put("path", path);
            return o.toString().getBytes(UTF8);
        } catch (JSONException e) {
            return new byte[0];
        }
    }

    // ---- connection loop ----------------------------------------------------

    private void closeOut() {
        OutputStream o = out;
        out = null;
        if (o != null) {
            try { o.close(); } catch (IOException ignored) {}
        }
    }

    private void runLoop() {
        int attempt = 0;
        while (!stop.get()) {
            try {
                serveOnce();
                attempt = 0; // a full session resets backoff
                if (stop.get()) break;
                listener.onDisconnected(true, "connection closed by host");
            } catch (RemoteProto.AuthException e) {
                listener.onAuthFailed(e.getMessage());
                listener.onDisconnected(false, e.getMessage());
                return;
            } catch (Exception e) {
                if (stop.get()) break;
                String msg = e.getMessage();
                listener.onDisconnected(true, msg == null ? "network error" : msg);
            }
            attempt++;
            long waitMs = Math.min(15000L, 1000L << Math.min(attempt, 4));
            try {
                Thread.sleep(waitMs);
            } catch (InterruptedException ie) {
                break;
            }
        }
        listener.onDisconnected(false, "stopped");
    }

    private void serveOnce() throws Exception {
        final Socket s = RemoteProto.connect(host, port, password, ctx);
        out = s.getOutputStream();
        listener.onConnected();
        net.execute(() -> pingLoop(s));
        final long[] winStart = { System.currentTimeMillis() };
        final int[] winFrames = { 0 };
        final long[] rtt = { -1 };
        try {
            while (!stop.get() && !s.isClosed()) {
                RemoteProto.Msg m = RemoteProto.recvMsg(s.getInputStream());
                if (m.type == RemoteProto.FRAME) {
                    winFrames[0]++;
                    listener.onFrame(m.payload);
                } else if (m.type == RemoteProto.PONG) {
                    byte[] tok = pingToken;
                    if (tok != null && Arrays.equals(tok, m.payload)) {
                        rtt[0] = System.currentTimeMillis() - pingSentAt;
                    }
                } else if (m.type == RemoteProto.CLIPBOARD_SET) {
                    dispatchClipboard(m.payload);
                } else if (m.type >= RemoteProto.FILE_LIST && m.type <= RemoteProto.FILE_ERROR) {
                    listener.onFileMsg(m.type, m.payload);
                } else if (m.type == RemoteProto.DISCONNECT) {
                    return;
                }
                long now = System.currentTimeMillis();
                if (now - winStart[0] >= 1000) {
                    listener.onStats(winFrames[0], rtt[0]);
                    winStart[0] = now;
                    winFrames[0] = 0;
                }
            }
        } finally {
            try { s.close(); } catch (IOException ignored) {}
            closeOut();
        }
    }

    private void dispatchClipboard(byte[] payload) {
        try {
            String text = new JSONObject(new String(payload, UTF8)).getString("text");
            listener.onClipboardText(text);
        } catch (JSONException ignored) {
        }
    }

    private void pingLoop(Socket s) {
        try {
            while (!stop.get() && !s.isClosed()) {
                Thread.sleep(4000);
                if (stop.get() || s.isClosed()) break;
                byte[] tok = new byte[8];
                random.nextBytes(tok);
                pingToken = tok;
                pingSentAt = System.currentTimeMillis();
                OutputStream o = out;
                if (o == null) break;
                RemoteProto.sendMsg(o, RemoteProto.PING, tok);
            }
        } catch (Exception ignored) {
        }
    }
}
