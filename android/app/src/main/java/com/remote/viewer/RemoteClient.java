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
        // v4 pairing: the host's policy demands a pairing code before
        // password auth. Default no-op so older listeners keep working.
        default void onPairingRequired() {}        void onFrame(byte[] jpeg);
        void onStats(int fps, long rttMs);
        void onClipboardText(String text);
        void onFileMsg(int type, byte[] payload);

        // v3 additions — default no-ops so v2-only listeners keep working.
        default void onProtoVersion(int version) {}
        default void onSystemResp(String cmd, boolean ok, String detail) {}
        default void onTerminalOpened(String session) {}
        default void onTerminalData(String session, byte[] data) {}
        default void onTerminalClosed(String session) {}
        default void onChat(String from, String text, long ts) {}
        default void onAgentStatus(String json) {}
        default void onDisplays(String json) {}
        default void onPermsResp(String deviceId, boolean ok, String detail) {}
        default void onPermsDenied(String op, String reason) {}
        default void onPermsList(String json) {}

        // v4 camera for verification — default no-ops so older listeners keep working.
        default void onCameraStatus(String json) {}
        default void onCameraStopReceived() {}
    }

    private final Context ctx;
    private final String host;
    private final int port;
    private final String password;
    private final Listener listener;
    /** Optional pairing code, sent in the pre-auth pairing window. */
    private volatile String pairCode;
    private final ExecutorService net = Executors.newCachedThreadPool();
    private final AtomicBoolean stop = new AtomicBoolean(false);
    private final SecureRandom random = new SecureRandom();

    private volatile OutputStream out;
    private volatile long pingSentAt;
    private volatile byte[] pingToken;
    private volatile int protoVersion = 1;
    private final java.util.concurrent.atomic.AtomicLong rxBytes =
            new java.util.concurrent.atomic.AtomicLong();
    private final java.util.concurrent.atomic.AtomicLong txBytes =
            new java.util.concurrent.atomic.AtomicLong();
    private volatile long sessionStartMs;

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

    /** Sets the pairing code used on the next (re)connect. Null/empty = none. */
    public void setPairCode(String code) {
        pairCode = code;
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
            txBytes.addAndGet(5L + (payload == null ? 0 : payload.length));
        } catch (IOException ignored) {
        }
    }

    /** Negotiated protocol version (REMOTE/N from AUTH_OK), 1 if unknown. */
    public int getProtoVersion() {
        return protoVersion;
    }

    public long getRxBytes() {
        return rxBytes.get();
    }

    public long getTxBytes() {
        return txBytes.get();
    }

    /** Uptime of the current connection in ms, 0 when disconnected. */
    public long getSessionAgeMs() {
        long s = sessionStartMs;
        return s == 0 ? 0 : System.currentTimeMillis() - s;
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

    // ---- v3 sends -----------------------------------------------------------

    /** 0x60 SYSTEM_CMD c->s {cmd, args?}. args may be null. */
    public void sendSystemCmd(String cmd, JSONObject args) {
        try {
            JSONObject o = new JSONObject();
            o.put("cmd", cmd);
            if (args != null) o.put("args", args);
            send(RemoteProto.SYSTEM_CMD, o.toString().getBytes(UTF8));
        } catch (JSONException ignored) {
        }
    }

    public void sendTerminalOpen(int cols, int rows) {
        try {
            JSONObject o = new JSONObject();
            o.put("cols", cols);
            o.put("rows", rows);
            send(RemoteProto.TERMINAL_OPEN, o.toString().getBytes(UTF8));
        } catch (JSONException ignored) {
        }
    }

    public void sendTerminalData(String session, byte[] data) {
        try {
            JSONObject o = new JSONObject();
            o.put("session", session);
            o.put("data", android.util.Base64.encodeToString(
                    data == null ? new byte[0] : data, android.util.Base64.NO_WRAP));
            send(RemoteProto.TERMINAL_DATA, o.toString().getBytes(UTF8));
        } catch (JSONException ignored) {
        }
    }

    public void sendTerminalClose(String session) {
        try {
            JSONObject o = new JSONObject();
            o.put("session", session);
            send(RemoteProto.TERMINAL_CLOSE, o.toString().getBytes(UTF8));
        } catch (JSONException ignored) {
        }
    }

    public void sendChat(String from, String text) {
        try {
            JSONObject o = new JSONObject();
            o.put("from", from == null ? "" : from);
            o.put("text", text == null ? "" : text);
            o.put("ts", System.currentTimeMillis());
            send(RemoteProto.CHAT_MSG, o.toString().getBytes(UTF8));
        } catch (JSONException ignored) {
        }
    }

    public void sendAgentQuery() {
        send(RemoteProto.AGENT_QUERY, new byte[0]);
    }

    public void sendDisplaysQuery() {
        send(RemoteProto.DISPLAYS_QUERY, "{}".getBytes(UTF8));
    }

    // ---- v3 permissions (host-enforced) -------------------------------------

    /** 0x80 PERMS_SET c->s {device_id, permissions:{view,mouse,...}}. */
    public void sendPermsSet(String deviceId, JSONObject permissions) {
        try {
            JSONObject o = new JSONObject();
            o.put("device_id", deviceId);
            o.put("permissions", permissions == null ? new JSONObject() : permissions);
            send(RemoteProto.PERMS_SET, o.toString().getBytes(UTF8));
        } catch (JSONException ignored) {
        }
    }

    /** 0x83 PERMS_LIST c->s. */
    public void sendPermsList() {
        send(RemoteProto.PERMS_LIST, "{}".getBytes(UTF8));
    }

    // ---- v4 camera for verification ---------------------------------------

    /**
     * 0x87 CAMERA_START c->s {width,height,fps,facing,rotation,mirror,codec}.
     * Sent only after the user explicitly taps Start Camera; the host
     * replies with CAMERA_STATUS (never auto-starts anything by itself).
     */
    public void sendCameraStart(int width, int height, int fps, String facing,
                                int rotation, boolean mirror) {
        try {
            JSONObject o = new JSONObject();
            o.put("width", width);
            o.put("height", height);
            o.put("fps", fps);
            o.put("facing", facing);
            o.put("rotation", rotation);
            o.put("mirror", mirror);
            o.put("codec", "h264");
            send(RemoteProto.CAMERA_START, o.toString().getBytes(UTF8));
        } catch (JSONException ignored) {
        }
    }

    /** 0x88 CAMERA_STOP c->s (empty). */
    public void sendCameraStop() {
        send(RemoteProto.CAMERA_STOP, new byte[0]);
    }

    /**
     * 0x89 CAMERA_FRAME c->s: one Annex-B access unit. Called from the
     * camera sender thread; RemoteProto.sendMsg serializes on the stream.
     */
    public void sendCameraFrame(byte[] accessUnit) {
        if (accessUnit == null || accessUnit.length == 0) return;
        send(RemoteProto.CAMERA_FRAME, accessUnit);
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
            } catch (RemoteProto.PairingRequiredException e) {
                listener.onPairingRequired();
                listener.onDisconnected(false, e.getMessage());
                return;
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
        final Socket s = RemoteProto.connect(host, port, password, pairCode, ctx);
        out = s.getOutputStream();
        sessionStartMs = System.currentTimeMillis();
        protoVersion = RemoteProto.lastProtoVersion;
        listener.onConnected();
        listener.onProtoVersion(protoVersion);
        net.execute(() -> pingLoop(s));
        final long[] winStart = { System.currentTimeMillis() };
        final int[] winFrames = { 0 };
        final long[] rtt = { -1 };
        try {
            while (!stop.get() && !s.isClosed()) {
                RemoteProto.Msg m = RemoteProto.recvMsg(s.getInputStream());
                rxBytes.addAndGet(5L + (m.payload == null ? 0 : m.payload.length));
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
                } else if (m.type == RemoteProto.SYSTEM_RESP) {
                    dispatchSystemResp(m.payload);
                } else if (m.type == RemoteProto.TERMINAL_OPEN) {
                    dispatchTerminalOpened(m.payload);
                } else if (m.type == RemoteProto.TERMINAL_DATA) {
                    dispatchTerminalData(m.payload);
                } else if (m.type == RemoteProto.TERMINAL_CLOSE) {
                    dispatchTerminalClosed(m.payload);
                } else if (m.type == RemoteProto.CHAT_MSG) {
                    dispatchChat(m.payload);
                } else if (m.type == RemoteProto.AGENT_STATUS) {
                    listener.onAgentStatus(new String(m.payload, UTF8));
                } else if (m.type == RemoteProto.DISPLAYS_LIST) {
                    listener.onDisplays(new String(m.payload, UTF8));
                } else if (m.type == RemoteProto.PERMS_RESP) {
                    dispatchPermsResp(m.payload);
                } else if (m.type == RemoteProto.PERMS_DENIED) {
                    dispatchPermsDenied(m.payload);
                } else if (m.type == RemoteProto.PERMS_LIST_RESP) {
                    listener.onPermsList(new String(m.payload, UTF8));
                } else if (m.type == RemoteProto.CAMERA_STATUS) {
                    listener.onCameraStatus(new String(m.payload, UTF8));
                } else if (m.type == RemoteProto.CAMERA_STOP) {
                    listener.onCameraStopReceived();
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
            sessionStartMs = 0;
        }
    }

    private void dispatchClipboard(byte[] payload) {
        try {
            String text = new JSONObject(new String(payload, UTF8)).getString("text");
            listener.onClipboardText(text);
        } catch (JSONException ignored) {
        }
    }

    private void dispatchSystemResp(byte[] payload) {
        try {
            JSONObject o = new JSONObject(new String(payload, UTF8));
            listener.onSystemResp(o.optString("cmd", ""),
                    o.optBoolean("ok", false), o.optString("detail", ""));
        } catch (JSONException ignored) {
        }
    }

    private void dispatchTerminalOpened(byte[] payload) {
        try {
            String session = new JSONObject(new String(payload, UTF8)).getString("session");
            listener.onTerminalOpened(session);
        } catch (JSONException ignored) {
        }
    }

    private void dispatchTerminalData(byte[] payload) {
        try {
            JSONObject o = new JSONObject(new String(payload, UTF8));
            String session = o.getString("session");
            byte[] data = android.util.Base64.decode(o.optString("data", ""),
                    android.util.Base64.DEFAULT);
            listener.onTerminalData(session, data);
        } catch (Exception ignored) {
        }
    }

    private void dispatchTerminalClosed(byte[] payload) {
        try {
            String session = new JSONObject(new String(payload, UTF8)).getString("session");
            listener.onTerminalClosed(session);
        } catch (JSONException ignored) {
        }
    }

    private void dispatchChat(byte[] payload) {
        try {
            JSONObject o = new JSONObject(new String(payload, UTF8));
            listener.onChat(o.optString("from", ""), o.optString("text", ""),
                    o.optLong("ts", System.currentTimeMillis()));
        } catch (JSONException ignored) {
        }
    }

    private void dispatchPermsResp(byte[] payload) {
        try {
            JSONObject o = new JSONObject(new String(payload, UTF8));
            listener.onPermsResp(o.optString("device_id", ""),
                    o.optBoolean("ok", false), o.optString("detail", ""));
        } catch (JSONException ignored) {
        }
    }

    private void dispatchPermsDenied(byte[] payload) {
        try {
            JSONObject o = new JSONObject(new String(payload, UTF8));
            listener.onPermsDenied(o.optString("op", ""), o.optString("reason", ""));
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
