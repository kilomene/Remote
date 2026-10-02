package com.remote.viewer;

import android.app.Activity;
import android.content.Intent;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.ImageButton;
import android.widget.TextView;

import com.remote.viewer.features.pairing.DeviceHello;

import org.json.JSONObject;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.net.SocketTimeoutException;
import java.nio.charset.StandardCharsets;

/**
 * Manual pairing-code entry (v4 pairing flow).
 *
 * The host shows a single-use 6-digit code; the user types it here.
 * This activity opens a socket, sends DEVICE_HELLO then PAIR_REQUEST
 * (0x77) {code, device_id, device_name, platform} pre-auth, and waits for
 * PAIR_RESULT (0x78) {ok, detail?}.
 *
 * On ok: the host is saved to the encrypted host store, marked trusted in
 * the Security Center, and the user proceeds to the normal password-auth
 * connect (SessionActivity, which reuses RemoteClient). The pairing socket
 * is closed before the session socket opens — pairing never skips auth.
 * On fail: the server's detail string is shown.
 */
public class PairActivity extends Activity {

    /** Builds the PAIR_REQUEST (0x77) JSON payload. Key order is fixed
     *  (code, device_id, device_name, platform) and is asserted byte-exact
     *  by tests/proto_v4_pair_client.py. */
    public static String buildPairRequestJson(String code, String deviceId,
                                              String deviceName) {
        try {
            JSONObject o = new JSONObject();
            o.put("code", code);
            o.put("device_id", deviceId == null ? "unknown" : deviceId);
            o.put("device_name", deviceName == null ? "" : deviceName);
            o.put("platform", "android");
            return o.toString();
        } catch (Exception e) {
            return "{\"code\":\"\",\"device_id\":\"unknown\","
                    + "\"device_name\":\"\",\"platform\":\"android\"}";
        }
    }

    private final Handler ui = new Handler(Looper.getMainLooper());
    private EditText hostF, portF, passF, codeF, nameF;
    private TextView status;
    private Button pairBtn;
    private volatile boolean pairing;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_pair);

        hostF = findViewById(R.id.pair_host);
        portF = findViewById(R.id.pair_port);
        passF = findViewById(R.id.pair_password);
        codeF = findViewById(R.id.pair_code);
        nameF = findViewById(R.id.pair_device_name);
        status = findViewById(R.id.pair_status);
        pairBtn = findViewById(R.id.btn_pair_go);

        nameF.setText(Build.MODEL);

        ImageButton back = findViewById(R.id.btn_pair_back);
        back.setOnClickListener(v -> finish());

        pairBtn.setOnClickListener(v -> startPairing());
    }

    private void setStatus(final String text, final int colorRes) {
        ui.post(() -> {
            if (text == null) {
                status.setVisibility(View.GONE);
            } else {
                status.setText(text);
                status.setTextColor(getResources().getColor(colorRes, null));
                status.setVisibility(View.VISIBLE);
            }
        });
    }

    private void startPairing() {
        if (pairing) return;
        final String host = hostF.getText().toString().trim();
        final String password = passF.getText().toString();
        final String code = codeF.getText().toString().trim();
        final String deviceName = nameF.getText().toString().trim();
        int port;
        try {
            port = Integer.parseInt(portF.getText().toString().trim());
        } catch (NumberFormatException e) {
            port = RemoteProto.PORT;
        }
        final int portFv = port;

        if (host.isEmpty() || password.isEmpty()) {
            setStatus(getString(R.string.pair_need_host), R.color.danger);
            return;
        }
        if (!code.matches("\\d{6}")) {
            setStatus(getString(R.string.pair_bad_code), R.color.danger);
            codeF.setError(getString(R.string.pair_bad_code));
            return;
        }

        pairing = true;
        ui.post(() -> pairBtn.setEnabled(false));
        setStatus(getString(R.string.pair_working), R.color.text_dim);

        new Thread(() -> {
            try {
                doPair(host, portFv, password, code, deviceName);
            } catch (Exception e) {
                String msg = e.getMessage();
                setStatus(msg == null ? "pairing failed" : msg, R.color.danger);
                finishPairing();
            }
        }, "pairing").start();
    }

    private void finishPairing() {
        pairing = false;
        ui.post(() -> pairBtn.setEnabled(true));
    }

    private void doPair(String host, int port, String password, String code,
                        String deviceName) throws Exception {
        Socket s = new Socket();
        try {
            s.connect(new InetSocketAddress(host, port), 10000);
            s.setTcpNoDelay(true);

            // 1. pre-auth device announcement (existing v2 behavior)
            DeviceHello.send(this, s);
            String deviceId = deviceIdFromHello();

            // 2. pairing-code redemption (v4)
            OutputStream out = s.getOutputStream();
            String req = buildPairRequestJson(code, deviceId, deviceName);
            RemoteProto.sendMsg(out, RemoteProto.PAIR_REQUEST,
                    req.getBytes(StandardCharsets.UTF_8));

            // 3. wait for the host's verdict
            s.setSoTimeout(15000);
            InputStream in = s.getInputStream();
            RemoteProto.Msg m;
            try {
                m = RemoteProto.recvMsg(in);
            } catch (SocketTimeoutException te) {
                throw new IOException("host did not answer the pairing request");
            }

            if (m.type == RemoteProto.PAIR_RESULT) {
                JSONObject o = new JSONObject(
                        new String(m.payload, StandardCharsets.UTF_8));
                boolean ok = o.optBoolean("ok", false);
                String detail = o.optString("detail", "");
                if (ok) {
                    onPaired(host, port, password);
                    return;
                }
                throw new IOException(detail.isEmpty()
                        ? "pairing rejected by host" : detail);
            }
            if (m.type == RemoteProto.AUTH_REQ) {
                // pre-v4 host: it answered DEVICE_HELLO with auth, it has no
                // idea what PAIR_REQUEST is.
                throw new IOException(getString(R.string.pair_legacy));
            }
            if (m.type == RemoteProto.AUTH_FAIL) {
                throw new IOException(new String(m.payload, StandardCharsets.UTF_8));
            }
            throw new IOException("unexpected reply 0x"
                    + Integer.toHexString(m.type));
        } finally {
            try { s.close(); } catch (IOException ignored) {}
        }
    }

    /** device_id must match the one sent in DEVICE_HELLO just before. */
    private String deviceIdFromHello() {
        try {
            JSONObject o = new JSONObject(DeviceHello.buildJson(this));
            String id = o.optString("device_id", "unknown");
            return id.isEmpty() ? "unknown" : id;
        } catch (Exception e) {
            return "unknown";
        }
    }

    /** Pairing accepted: persist the host, mark it trusted, then connect
     *  with the normal password-auth session (RemoteClient inside
     *  SessionActivity opens its own socket). */
    private void onPaired(String host, int port, String password) {
        String label = host;
        new SecureStore(this).upsert(
                new SecureStore.Host(label, host, port, password));
        SecurityCenterActivity.markTrusted(this, host + ":" + port);
        setStatus(getString(R.string.pair_paired), R.color.success);
        finishPairing();
        ui.post(() -> {
            Intent i = new Intent(this, SessionActivity.class);
            i.putExtra(SessionActivity.EXTRA_HOST, host);
            i.putExtra(SessionActivity.EXTRA_PORT, port);
            i.putExtra(SessionActivity.EXTRA_PASSWORD, password);
            i.putExtra("device_label", label);
            startActivity(i);
            finish();
        });
    }
}
