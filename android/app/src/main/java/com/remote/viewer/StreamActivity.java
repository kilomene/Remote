package com.remote.viewer;

import android.app.Activity;
import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.inputmethod.InputMethodManager;
import android.widget.Button;
import android.widget.TextView;
import android.widget.Toast;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.Socket;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** Live remote-desktop session: shows frames, forwards touch/keyboard input. */
public class StreamActivity extends Activity implements StreamView.InputListener {

    public static final String EXTRA_HOST = "host";
    public static final String EXTRA_PORT = "port";
    public static final String EXTRA_PASSWORD = "password";

    private StreamView view;
    private TextView status;
    private volatile Socket socket;
    private volatile OutputStream out;
    private volatile boolean running;
    private ExecutorService sender;
    private final Handler ui = new Handler(Looper.getMainLooper());

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_stream);
        view = findViewById(R.id.stream_view);
        status = findViewById(R.id.status);
        view.setInputListener(this);

        Button kb = findViewById(R.id.btn_keyboard);
        kb.setOnClickListener(v -> {
            InputMethodManager imm = (InputMethodManager) getSystemService(Context.INPUT_METHOD_SERVICE);
            view.requestFocus();
            imm.showSoftInput(view, 0);
        });
        Button disc = findViewById(R.id.btn_disconnect);
        disc.setOnClickListener(v -> finish());

        sender = Executors.newSingleThreadExecutor();
        String host = getIntent().getStringExtra(EXTRA_HOST);
        int port = getIntent().getIntExtra(EXTRA_PORT, RemoteProto.PORT);
        String password = getIntent().getStringExtra(EXTRA_PASSWORD);
        connect(host, port, password);
    }

    private void connect(final String host, final int port, final String password) {
        setStatus("connecting to " + host + ":" + port + " …");
        new Thread(() -> {
            try {
                Socket s = RemoteProto.connect(host, port, password);
                socket = s;
                out = s.getOutputStream();
                running = true;
                setStatus("connected");
                receiveLoop(s.getInputStream());
            } catch (final Exception e) {
                setStatus("error: " + e.getMessage());
                ui.post(() -> Toast.makeText(StreamActivity.this,
                        "Connection failed: " + e.getMessage(), Toast.LENGTH_LONG).show());
            }
        }).start();
    }

    private void receiveLoop(InputStream in) {
        try {
            while (running) {
                RemoteProto.Msg m = RemoteProto.recvMsg(in);
                if (m.type == RemoteProto.FRAME) {
                    final Bitmap bmp = BitmapFactory.decodeByteArray(
                            m.payload, 0, m.payload.length);
                    if (bmp != null) ui.post(() -> view.setFrame(bmp));
                }
                // PONG / others: ignored in v1
            }
        } catch (IOException e) {
            if (running) setStatus("disconnected: " + e.getMessage());
        }
    }

    private void setStatus(final String s) {
        ui.post(() -> status.setText(s));
    }

    // ---- StreamView.InputListener -> Remote INPUT events -------------------------

    private void send(final String json) {
        final OutputStream o = out;
        if (o == null) return;
        sender.execute(() -> {
            try {
                RemoteProto.sendInput(o, json);
            } catch (IOException ignored) {}
        });
    }

    private static String esc(String s) {
        return s.replace("\\", "\\\\").replace("\"", "\\\"");
    }

    @Override public void onMove(int x, int y) {
        send("{\"t\":\"move\",\"x\":" + x + ",\"y\":" + y + "}");
    }

    @Override public void onTap(int x, int y) {
        onMove(x, y);
        send("{\"t\":\"click\",\"button\":\"left\",\"down\":true}");
        send("{\"t\":\"click\",\"button\":\"left\",\"down\":false}");
    }

    @Override public void onRightClick(int x, int y) {
        onMove(x, y);
        send("{\"t\":\"click\",\"button\":\"right\",\"down\":true}");
        send("{\"t\":\"click\",\"button\":\"right\",\"down\":false}");
        ui.post(() -> Toast.makeText(this, "right-click", Toast.LENGTH_SHORT).show());
    }

    @Override public void onKey(String key, boolean down) {
        send("{\"t\":\"key\",\"key\":\"" + esc(key) + "\",\"down\":" + down + "}");
    }

    @Override public void onText(char c) {
        if (Character.isUpperCase(c)) {
            onKey("Shift_L", true);
            onKey(String.valueOf(Character.toLowerCase(c)), true);
            onKey(String.valueOf(Character.toLowerCase(c)), false);
            onKey("Shift_L", false);
        } else {
            onKey(String.valueOf(c), true);
            onKey(String.valueOf(c), false);
        }
    }

    @Override
    protected void onDestroy() {
        running = false;
        try {
            if (out != null) RemoteProto.sendMsg(out, RemoteProto.DISCONNECT, new byte[0]);
        } catch (IOException ignored) {}
        try {
            if (socket != null) socket.close();
        } catch (IOException ignored) {}
        sender.shutdownNow();
        super.onDestroy();
    }
}
