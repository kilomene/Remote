package com.remote.viewer;

import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.View;
import android.view.WindowManager;
import android.view.inputmethod.InputMethodManager;
import android.widget.Button;
import android.widget.ImageButton;
import android.widget.LinearLayout;
import android.widget.PopupMenu;
import android.widget.TextView;
import android.widget.Toast;

import com.remote.viewer.features.clipboard.ClipboardSync;

import org.json.JSONObject;

/**
 * Live remote-desktop session: wires StreamView <-> RemoteClient, plus
 * clipboard sync, quality control, reconnect overlay and file manager entry.
 */
public class SessionActivity extends Activity
        implements RemoteClient.Listener, StreamView.InputListener {

    public static final String EXTRA_HOST = "host";
    public static final String EXTRA_PORT = "port";
    public static final String EXTRA_PASSWORD = "password";

    private StreamView streamView;
    private TextView stats;
    private LinearLayout statusOverlay;
    private TextView statusText;
    private Button btnStopReconnect;

    private RemoteClient client;
    private ClipboardSync clipboardSync;
    private Prefs prefs;
    private final Handler ui = new Handler(Looper.getMainLooper());

    private String host;
    private int port;
    private String password;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        prefs = Prefs.load(this);

        // edge-to-edge
        getWindow().getDecorView().setSystemUiVisibility(
                View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                        | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                        | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION);
        if (prefs.keepAwake) {
            getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        }

        setContentView(R.layout.activity_session);

        host = getIntent().getStringExtra(EXTRA_HOST);
        port = getIntent().getIntExtra(EXTRA_PORT, RemoteProto.PORT);
        password = getIntent().getStringExtra(EXTRA_PASSWORD);
        if (host == null || password == null) {
            Toast.makeText(this, "missing connection details", Toast.LENGTH_LONG).show();
            finish();
            return;
        }

        streamView = findViewById(R.id.stream_view);
        stats = findViewById(R.id.stats);
        statusOverlay = findViewById(R.id.status_overlay);
        statusText = findViewById(R.id.status_text);
        btnStopReconnect = findViewById(R.id.btn_stop_reconnect);

        streamView.applyPrefs(prefs);
        streamView.setInputListener(this);

        ImageButton btnDisconnect = findViewById(R.id.btn_disconnect);
        btnDisconnect.setOnClickListener(v -> disconnectAndFinish());

        ImageButton btnKeyboard = findViewById(R.id.btn_keyboard);
        btnKeyboard.setOnClickListener(v -> {
            InputMethodManager imm =
                    (InputMethodManager) getSystemService(Context.INPUT_METHOD_SERVICE);
            streamView.requestFocus();
            if (imm != null) imm.showSoftInput(streamView, 0);
        });

        ImageButton btnQuality = findViewById(R.id.btn_quality);
        btnQuality.setOnClickListener(v -> showQualityPopup(btnQuality));

        ImageButton btnFiles = findViewById(R.id.btn_files);
        btnFiles.setOnClickListener(v -> {
            Intent i = new Intent(this, FileManagerActivity.class);
            i.putExtra(FileManagerActivity.EXTRA_HOST, host);
            i.putExtra(FileManagerActivity.EXTRA_PORT, port);
            i.putExtra(FileManagerActivity.EXTRA_PASSWORD, password);
            startActivity(i);
        });

        btnStopReconnect.setOnClickListener(v -> disconnectAndFinish());

        clipboardSync = new ClipboardSync(this, new ClipboardSync.Callback() {
            @Override
            public void onLocalClipboardChanged(String text) {
                if (client != null) client.sendClipboard(text);
            }
        });
        clipboardSync.setEnabled(prefs.clipboard);
        clipboardSync.start();

        showStatus(getString(R.string.reconnecting), false);
        client = new RemoteClient(this, host, port, password, this);
        client.start();
    }

    private void showQualityPopup(View anchor) {
        PopupMenu menu = new PopupMenu(this, anchor);
        int[] caps = {12, 24, 60};
        for (int cap : caps) {
            menu.getMenu().add(0, cap, 0, cap + " fps");
        }
        menu.setOnMenuItemClickListener(item -> {
            int cap = item.getItemId();
            prefs = Prefs.load(this);
            Prefs.save(this, prefs.haptics, prefs.keepAwake, prefs.smoothScaling,
                    prefs.clipboard, cap);
            prefs = Prefs.load(this);
            streamView.applyPrefs(prefs);
            Toast.makeText(this, cap + " fps", Toast.LENGTH_SHORT).show();
            return true;
        });
        menu.show();
    }

    private void showStatus(String text, boolean showStop) {
        ui.post(() -> {
            statusText.setText(text);
            btnStopReconnect.setVisibility(showStop ? View.VISIBLE : View.GONE);
            statusOverlay.setVisibility(View.VISIBLE);
        });
    }

    private void hideStatus() {
        ui.post(() -> statusOverlay.setVisibility(View.GONE));
    }

    private void disconnectAndFinish() {
        if (client != null) {
            client.sendDisconnect();
            client.stop();
            client = null;
        }
        finish();
    }

    // ---- RemoteClient.Listener ----------------------------------------------

    @Override
    public void onConnected() {
        hideStatus();
    }

    @Override
    public void onAuthFailed(final String reason) {
        ui.post(() -> {
            Toast.makeText(SessionActivity.this,
                    getString(R.string.auth_failed), Toast.LENGTH_LONG).show();
            finish();
        });
    }

    @Override
    public void onDisconnected(final boolean willRetry, final String reason) {
        if (willRetry) {
            showStatus(getString(R.string.reconnecting), true);
        } else {
            hideStatus();
        }
    }

    @Override
    public void onFrame(byte[] jpeg) {
        streamView.submitFrame(jpeg);
    }

    @Override
    public void onStats(final int fps, final long rttMs) {
        ui.post(() -> {
            String rtt = rttMs < 0 ? "—" : rttMs + " ms";
            stats.setText(fps + " fps · " + rtt);
        });
    }

    @Override
    public void onClipboardText(String text) {
        if (clipboardSync != null) clipboardSync.applyRemoteText(text);
    }

    @Override
    public void onFileMsg(int type, byte[] payload) {
        // The session's own connection carries no file ops; FileManagerActivity
        // opens its own connection. Ignore.
    }

    // ---- StreamView.InputListener -> Remote INPUT events ---------------------

    private void send(String json) {
        RemoteClient c = client;
        if (c != null) c.sendInput(json);
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
    }

    @Override public void onScroll(int dx, int dy) {
        send("{\"t\":\"scroll\",\"dx\":" + dx + ",\"dy\":" + dy + "}");
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
        if (clipboardSync != null) {
            clipboardSync.stop();
            clipboardSync = null;
        }
        if (streamView != null) streamView.shutdown();
        if (client != null) {
            client.stop();
            client = null;
        }
        super.onDestroy();
    }
}
