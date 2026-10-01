package com.remote.viewer;

import com.remote.viewer.features.pairing.DeviceHello;

import java.io.ByteArrayOutputStream;
import java.io.EOFException;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;

import javax.crypto.Mac;
import javax.crypto.SecretKeyFactory;
import javax.crypto.spec.PBEKeySpec;
import javax.crypto.spec.SecretKeySpec;

/**
 * Remote wire protocol v1 (see PROTOCOL.md).
 * Framing: [1-byte type][4-byte big-endian length][payload].
 * Auth: server sends salt(16)||nonce(32); client replies
 * HMAC-SHA256(PBKDF2-HMAC-SHA256(password, salt, 200000), nonce).
 */
public final class RemoteProto {
    public static final int PORT = 47800;

    public static final int AUTH_REQ = 0x01;
    public static final int AUTH_RESP = 0x02;
    public static final int AUTH_OK = 0x03;
    public static final int AUTH_FAIL = 0x04;
    public static final int FRAME = 0x10;
    public static final int INPUT = 0x11;
    public static final int PING = 0x20;
    public static final int PONG = 0x21;
    public static final int DISCONNECT = 0xFF;

    // v2 additions (see PROTOCOL.md). All v1 types above are byte-identical.
    public static final int DEVICE_HELLO = 0x30;
    public static final int CLIPBOARD_SET = 0x40;
    public static final int FILE_LIST = 0x50;
    public static final int FILE_GET = 0x51;
    public static final int FILE_DATA = 0x52;
    public static final int FILE_DONE = 0x53;
    public static final int FILE_PUT = 0x54;
    public static final int FILE_MKDIR = 0x55;
    public static final int FILE_DELETE = 0x56;
    public static final int FILE_RENAME = 0x57;
    public static final int FILE_LIST_RESP = 0x58;
    public static final int FILE_META = 0x59;
    public static final int FILE_ERROR = 0x5A;

    private static final int PBKDF2_ITERATIONS = 200000;

    public static final class Msg {
        public final int type;
        public final byte[] payload;
        Msg(int type, byte[] payload) { this.type = type; this.payload = payload; }
    }

    public static class AuthException extends IOException {
        AuthException(String m) { super(m); }
    }

    private RemoteProto() {}

    public static void sendMsg(OutputStream out, int type, byte[] payload) throws IOException {
        ByteBuffer h = ByteBuffer.allocate(5).order(ByteOrder.BIG_ENDIAN);
        h.put((byte) type);
        h.putInt(payload.length);
        synchronized (out) {
            out.write(h.array());
            out.write(payload);
            out.flush();
        }
    }

    private static byte[] readN(InputStream in, int n) throws IOException {
        ByteArrayOutputStream buf = new ByteArrayOutputStream(n);
        byte[] tmp = new byte[Math.min(8192, Math.max(n, 1))];
        int left = n;
        while (left > 0) {
            int r = in.read(tmp, 0, Math.min(tmp.length, left));
            if (r < 0) throw new EOFException("connection closed");
            buf.write(tmp, 0, r);
            left -= r;
        }
        return buf.toByteArray();
    }

    public static Msg recvMsg(InputStream in) throws IOException {
        byte[] h = readN(in, 5);
        ByteBuffer b = ByteBuffer.wrap(h).order(ByteOrder.BIG_ENDIAN);
        int type = b.get() & 0xFF;
        int len = b.getInt();
        if (len < 0 || len > 8 * 1024 * 1024) throw new IOException("bad payload length " + len);
        return new Msg(type, readN(in, len));
    }

    /** Connects and runs the client side of the auth handshake. Returns the socket.
     *  No DEVICE_HELLO is sent (v1 behavior). */
    public static Socket connect(String host, int port, String password) throws Exception {
        return connect(host, port, password, null);
    }

    /**
     * Connects and runs the client side of the auth handshake. Returns the socket.
     * When {@code ctx} is non-null, a v2 DEVICE_HELLO is sent immediately after
     * TCP connect and before auth (see PROTOCOL.md); a null ctx skips it.
     */
    public static Socket connect(String host, int port, String password,
                                 android.content.Context ctx) throws Exception {
        Socket s = new Socket();
        s.connect(new InetSocketAddress(host, port), 10000);
        s.setTcpNoDelay(true);
        try {
            if (ctx != null) {
                DeviceHello.send(ctx, s);
            }
            InputStream in = s.getInputStream();
            OutputStream out = s.getOutputStream();
            Msg m = recvMsg(in);
            if (m.type == AUTH_FAIL) {
                throw new AuthException("server refused: " + new String(m.payload, StandardCharsets.UTF_8));
            }
            if (m.type != AUTH_REQ || m.payload.length != 48) {
                throw new AuthException("bad AUTH_REQ from server");
            }
            byte[] salt = Arrays.copyOfRange(m.payload, 0, 16);
            byte[] nonce = Arrays.copyOfRange(m.payload, 16, 48);
            SecretKeyFactory kf = SecretKeyFactory.getInstance("PBKDF2WithHmacSHA256");
            byte[] key = kf.generateSecret(
                    new PBEKeySpec(password.toCharArray(), salt, PBKDF2_ITERATIONS, 256)).getEncoded();
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(key, "HmacSHA256"));
            sendMsg(out, AUTH_RESP, mac.doFinal(nonce));
            // best-effort: don't keep password-derived material longer than needed
            Arrays.fill(key, (byte) 0);
            m = recvMsg(in);
            if (m.type == AUTH_OK) return s;
            if (m.type == AUTH_FAIL) {
                throw new AuthException("auth failed: " + new String(m.payload, StandardCharsets.UTF_8));
            }
            throw new AuthException("unexpected reply 0x" + Integer.toHexString(m.type));
        } catch (Exception e) {
            try { s.close(); } catch (IOException ignored) {}
            throw e;
        }
    }

    public static void sendInput(OutputStream out, String json) throws IOException {
        sendMsg(out, INPUT, json.getBytes(StandardCharsets.UTF_8));
    }
}
