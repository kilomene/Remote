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
 * Remote wire protocol v1/v2/v3/v4 (see PROTOCOL.md).
 * Framing: [1-byte type][4-byte big-endian length][payload].
 * Auth: server sends salt(16)||nonce(32); client replies
 * HMAC-SHA256(PBKDF2-HMAC-SHA256(password, salt, 200000), nonce).
 *
 * v4 adds (see PROTOCOL.md), all byte-identical with the host:
 * TERMINAL_RESIZE (0x69), AUDIO_START/DATA/STOP/ERROR (0x70-0x73),
 * WEBCAM_LIST/FRAME (0x74/0x75), NET_STATUS (0x76),
 * PAIR_REQUEST/RESULT/REQUIRED (0x77-0x79), EXEC_RUN/RESULT (0x7A/0x7B),
 * POLICY_GET (0x7C), SESSION_TOKEN/ROTATE (0x85/0x86),
 * CAMERA_START/STOP/FRAME/STATUS (0x87-0x8A).
 * AUTH_OK carries b"REMOTE/N"; any REMOTE/N is accepted and the version
 * is exposed via {@link #lastProtoVersion} for graceful degradation.
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

    // v3 additions (see PROTOCOL.md).
    public static final int SYSTEM_CMD = 0x60;
    public static final int TERMINAL_OPEN = 0x61;    // c->s {cols,rows}; s->c {session}
    public static final int TERMINAL_DATA = 0x62;    // bidi {session, data: base64}
    public static final int TERMINAL_CLOSE = 0x63;   // bidi {session}
    public static final int CHAT_MSG = 0x64;         // bidi {from, text, ts}
    public static final int AGENT_QUERY = 0x65;      // c->s (empty)
    public static final int AGENT_STATUS = 0x66;     // s->c {cpu_pct, mem_*, disk_*, net_*, services, ts}
    public static final int DISPLAYS_QUERY = 0x67;   // c->s (empty)
    public static final int DISPLAYS_LIST = 0x68;    // s->c {displays:[{id,name,w,h,primary}], active}
    public static final int SYSTEM_RESP = 0x6F;      // s->c {cmd, ok, detail}

    // 0x70-0x7F: v4 block (see PROTOCOL.md; byte-identical with the host).
    // Audio is real host-side in v4; the Android playback UI ships later.
    public static final int AUDIO_START = 0x70;   // c->s {source}
    public static final int AUDIO_DATA = 0x71;    // s->c raw audio (12-byte header)
    public static final int AUDIO_STOP = 0x72;    // bidi (empty)
    public static final int AUDIO_ERROR = 0x73;   // s->c {detail}
    public static final int WEBCAM_LIST = 0x74;   // c->s {} / s->c {cameras:[{id,name}]}
    public static final int WEBCAM_FRAME = 0x75;  // c->s {id} / s->c JPEG bytes
    public static final int NET_STATUS = 0x76;    // c->s {} / s->c {online, tailscale_ip,
                                                  //   peer_latency_ms, direct}
    public static final int PAIR_REQUEST = 0x77;  // c->s {code, device_id, device_name, platform}
    public static final int PAIR_RESULT = 0x78;   // s->c {ok, detail?}
    public static final int PAIR_REQUIRED = 0x79;// s->c {device_id}
    public static final int EXEC_RUN = 0x7A;      // c->s {name, args:[]} allowlisted automation
    public static final int EXEC_RESULT = 0x7B;   // s->c {name, ok, output, error?}
    public static final int POLICY_GET = 0x7C;    // c->s {} / s->c {toggles}

    // v4: terminal resize fills the old v3 spare slot 0x69.
    public static final int TERMINAL_RESIZE = 0x69; // c->s {session, cols, rows}

    // v3 multi-user permissions (host-enforced).
    public static final int PERMS_SET = 0x80;        // c->s {device_id, permissions:{...}}
    public static final int PERMS_RESP = 0x81;       // s->c {device_id, ok, detail}
    public static final int PERMS_DENIED = 0x82;     // s->c {op, reason}
    public static final int PERMS_LIST = 0x83;       // c->s (empty)
    public static final int PERMS_LIST_RESP = 0x84;  // s->c {devices:[{device_id, device_name,
                                                    //   platform, first_seen, last_seen,
                                                    //   permissions:{...}}]}

    // v4 session tokens (0x85-0x86), s->c {token, expires_in}
    public static final int SESSION_TOKEN = 0x85;
    public static final int SESSION_ROTATE = 0x86;

    // v4 camera for verification (0x87-0x8A). Host side is real in v4;
    // the Android capture side ships in a later release.
    public static final int CAMERA_START = 0x87;   // c->s {width, height, fps, facing}
    public static final int CAMERA_STOP = 0x88;    // bidi (empty)
    public static final int CAMERA_FRAME = 0x89;   // c->s raw H264 Annex-B
    public static final int CAMERA_STATUS = 0x8A;  // s->c {active, device, width, height,
                                                   //   fps, error?}

    /** The 12 permission flags the host enforces per trusted device. */
    public static final String[] PERMISSIONS = {
            "view", "mouse", "keyboard", "clipboard", "files", "terminal",
            "system", "audio", "webcam", "apps", "automation", "camera"};

    /** Protocol version from the last successful AUTH_OK (REMOTE/N). */
    public static volatile int lastProtoVersion = 1;

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
            if (m.type == AUTH_OK) {
                lastProtoVersion = parseVersion(m.payload);
                return s;
            }
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

    /**
     * Parses b"REMOTE/N" into N. Any payload starting with "REMOTE/" is
     * accepted (forward compatible); unparseable -> 1 (degrade to v1).
     */
    public static int parseVersion(byte[] payload) {
        try {
            String s = new String(payload, StandardCharsets.UTF_8).trim();
            if (s.startsWith("REMOTE/")) {
                return Integer.parseInt(s.substring(7).trim());
            }
        } catch (Exception ignored) {
        }
        return 1;
    }
}
