package com.remote.viewer;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ActivityInfo;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
import android.view.View;
import android.view.WindowManager;
import android.view.inputmethod.InputMethodManager;
import android.widget.ArrayAdapter;
import android.widget.Button;
import android.widget.EditText;
import android.widget.HorizontalScrollView;
import android.widget.ImageButton;
import android.widget.LinearLayout;
import android.widget.ListView;
import android.widget.PopupMenu;
import android.widget.TextView;
import android.widget.Toast;

import com.remote.viewer.features.camera.CameraProtocol;
import com.remote.viewer.features.camera.CameraStreamer;
import com.remote.viewer.features.clipboard.ClipboardHistory;
import com.remote.viewer.features.clipboard.ClipboardSync;
import com.remote.viewer.features.notify.Notify;
import com.remote.viewer.features.recording.ScreenRecorder;
import com.remote.viewer.features.recording.Screenshots;
import com.remote.viewer.features.session.SessionDb;
import com.remote.viewer.features.system.SystemActions;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicLong;

/**
 * Live remote-desktop session: StreamView <-> RemoteClient, plus the full
 * v3 feature set — control modes, keyboard toolbar, quality/FPS/adaptive,
 * fullscreen/orientation/scale, displays, privacy, quick actions, chat,
 * clipboard history, screenshots, screen recording, connection panel,
 * session timeout, and session-history recording.
 */
public class SessionActivity extends Activity
        implements RemoteClient.Listener, StreamView.InputListener {

    public static final String EXTRA_HOST = "host";
    public static final String EXTRA_PORT = "port";
    public static final String EXTRA_PASSWORD = "password";

    // quality presets: {fpsCap, smooth, decodeSample}
    private static final String[] QUALITIES = {"ultra", "high", "balanced", "low", "minlat"};
    private static final int[] QUALITY_FPS = {60, 45, 30, 15, 60};
    private static final boolean[] QUALITY_SMOOTH = {true, true, true, true, false};
    private static final int[] QUALITY_SAMPLE = {1, 1, 1, 2, 1};

    private StreamView streamView;
    private TextView stats;
    private TextView adaptiveBadge;
    private LinearLayout statusOverlay;
    private TextView statusText;
    private Button btnStopReconnect;
    private LinearLayout toolbar;
    private HorizontalScrollView toolbarScroll;
    private HorizontalScrollView keybarScroll;
    private LinearLayout keybar;
    private LinearLayout mouseBar;
    private ImageButton btnExitFs;

    // drawers
    private LinearLayout chatDrawer;
    private ListView chatList;
    private EditText chatInput;
    private final List<String> chatRows = new ArrayList<String>();
    private ArrayAdapter<String> chatAdapter;
    private int chatUnread;
    private ImageButton chatBtn;

    private LinearLayout clipDrawer;
    private ListView clipList;
    private ArrayAdapter<String> clipAdapter;
    private final List<String> clipRows = new ArrayList<String>();

    // recording
    private LinearLayout recIndicator;
    private TextView recTime;
    private ScreenRecorder recorder;
    // camera for verification (v4): phone camera -> host /dev/videoN.
    // Privacy: started ONLY by explicit user tap; never auto-starts.
    private LinearLayout camIndicator;
    private TextView camStatusText;
    private CameraStreamer cameraStreamer;
    private final BlockingQueue<byte[]> camQueue =
            new ArrayBlockingQueue<byte[]>(4);
    private Thread camSenderThread;
    private final AtomicBoolean camSending = new AtomicBoolean(false);
    private final AtomicLong camDropped = new AtomicLong(0);
    private volatile boolean cameraPendingStart; // CAMERA_START sent, awaiting status
    private int camPendW, camPendH, camPendFps, camPendRotation;
    private String camPendFacing = "rear";
    private boolean camPendMirror;
    private static final int REQ_CAMERA_PERM = 1401;    private final Handler ui = new Handler(Looper.getMainLooper());
    private long recStartMs;
    private long pauseStartMs; // wall time when the current pause began (0 when not paused)
    private final Runnable recTicker = new Runnable() {
        @Override public void run() {
            if (recorder == null) return;
            if (!recorder.isPaused()) {
                Bitmap c = streamView.snapshotCopy();
                if (c != null) {
                    try {
                        recorder.feedFrame(c);
                    } finally {
                        c.recycle();
                    }
                }
                long s = (System.currentTimeMillis() - recStartMs) / 1000;
                recTime.setText(String.format(Locale.US, "%02d:%02d", s / 60, s % 60));
            }
            ui.postDelayed(this, 66);
        }
    };

    private RemoteClient client;
    private ClipboardSync clipboardSync;
    private Prefs prefs;

    private String host;
    private int port;
    private String password;
    private String deviceLabel;

    private boolean fullscreen;
    private int orientationIdx; // 0 sensor, 1 landscape, 2 portrait
    private boolean privacyOn;
    private String displaysJson;
    private long sessionDbId = -1;
    private boolean connected;

    // adaptive quality state
    private final long[] rttWin = new long[3];
    private int rttCount;
    private int rttGoodStreak;
    private boolean adaptiveReduced;
    // Badge shows once per session, quietly. The quality still adapts on
    // later latency spikes, but the banner never pops again.
    private boolean adaptiveBadgeShown;

    // session timeout
    private final Runnable timeoutFire = new Runnable() {
        @Override public void run() {
            Toast.makeText(SessionActivity.this,
                    R.string.session_timeout_fired, Toast.LENGTH_LONG).show();
            disconnectAndFinish();
        }
    };

    // sticky modifiers for the key toolbar
    private final Set<String> stickyMods = new HashSet<String>();
    private final List<Button> modButtons = new ArrayList<Button>();

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        prefs = Prefs.load(this);
        deviceLabel = getIntent().getStringExtra("device_label");

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
        if (deviceLabel == null) deviceLabel = host;

        streamView = findViewById(R.id.stream_view);
        stats = findViewById(R.id.stats);
        adaptiveBadge = findViewById(R.id.adaptive_badge);
        statusOverlay = findViewById(R.id.status_overlay);
        statusText = findViewById(R.id.status_text);
        btnStopReconnect = findViewById(R.id.btn_stop_reconnect);
        toolbar = findViewById(R.id.toolbar);
        toolbarScroll = findViewById(R.id.toolbar_scroll);
        keybarScroll = findViewById(R.id.keybar_scroll);
        keybar = findViewById(R.id.keybar);
        mouseBar = findViewById(R.id.mouse_bar);
        recIndicator = findViewById(R.id.rec_indicator);
        recTime = findViewById(R.id.rec_time);
        camIndicator = findViewById(R.id.cam_indicator);
        camStatusText = findViewById(R.id.cam_status_text);
        camIndicator.setOnClickListener(v -> showCameraPanel());
        cameraStreamer = new CameraStreamer();

        chatDrawer = findViewById(R.id.chat_drawer);
        chatList = findViewById(R.id.chat_list);
        chatInput = findViewById(R.id.chat_input);
        chatAdapter = new ArrayAdapter<String>(this,
                android.R.layout.simple_list_item_1, chatRows);
        chatList.setAdapter(chatAdapter);

        clipDrawer = findViewById(R.id.clip_drawer);
        clipList = findViewById(R.id.clip_list);
        clipAdapter = new ArrayAdapter<String>(this,
                android.R.layout.simple_list_item_1, clipRows);
        clipList.setAdapter(clipAdapter);

        streamView.applyPrefs(prefs);
        streamView.setInputListener(this);
        applyQuality(Prefs.getString(this, Prefs.K_QUALITY, "balanced"), false);
        updateMouseBar();

        buildToolbar();
        buildKeybar();

        btnExitFs = new ImageButton(this);
        btnExitFs.setImageResource(R.drawable.ic_fullscreen);
        btnExitFs.setBackgroundResource(R.drawable.bg_icon_btn);
        btnExitFs.setContentDescription(getString(R.string.exit_fullscreen));
        android.widget.FrameLayout.LayoutParams fsp =
                new android.widget.FrameLayout.LayoutParams(
                        (int) (48 * getResources().getDisplayMetrics().density),
                        (int) (48 * getResources().getDisplayMetrics().density));
        fsp.gravity = android.view.Gravity.TOP | android.view.Gravity.START;
        int m = (int) (12 * getResources().getDisplayMetrics().density);
        fsp.setMargins(m, m, 0, 0);
        btnExitFs.setLayoutParams(fsp);
        btnExitFs.setVisibility(View.GONE);
        btnExitFs.setOnClickListener(v -> setFullscreen(false));
        ((android.widget.FrameLayout) findViewById(android.R.id.content)).addView(btnExitFs);

        findViewById(R.id.chat_send).setOnClickListener(v -> sendChat());
        findViewById(R.id.clip_clear).setOnClickListener(v -> {
            ClipboardHistory.clear(this);
            refreshClipDrawer();
        });
        clipList.setOnItemClickListener((parent, view, position, id) -> {
            String text = clipRows.get(position);
            ClipboardManager cm = (ClipboardManager)
                    getSystemService(Context.CLIPBOARD_SERVICE);
            if (cm != null) cm.setPrimaryClip(ClipData.newPlainText("remote", text));
            if (client != null) client.sendClipboard(text);
            Toast.makeText(this, R.string.clip_sent, Toast.LENGTH_SHORT).show();
        });

        findViewById(R.id.mouse_left).setOnClickListener(v -> streamView.clickAtCursor(false));
        findViewById(R.id.mouse_right).setOnClickListener(v -> streamView.clickAtCursor(true));

        btnStopReconnect.setOnClickListener(v -> disconnectAndFinish());

        clipboardSync = new ClipboardSync(this, new ClipboardSync.Callback() {
            @Override
            public void onLocalClipboardChanged(String text) {
                if (client != null) client.sendClipboard(text);
                ClipboardHistory.add(SessionActivity.this, text);
                if (clipDrawer.getVisibility() == View.VISIBLE) refreshClipDrawer();
            }
        });
        clipboardSync.setEnabled(prefs.clipboard);
        clipboardSync.start();

        // session timeout
        int timeoutMin = Prefs.getInt(this, Prefs.K_SESSION_TIMEOUT_MIN, 0);
        if (timeoutMin > 0) {
            ui.postDelayed(timeoutFire, timeoutMin * 60L * 1000L);
        }

        showStatus(getString(R.string.reconnecting), false);
        client = new RemoteClient(this, host, port, password, this);
        client.start();
    }

    // ---- toolbar ------------------------------------------------------------

    private ImageButton toolBtn(int icon, String desc, View.OnClickListener l) {
        ImageButton b = new ImageButton(this);
        b.setImageResource(icon);
        b.setBackgroundResource(R.drawable.bg_icon_btn);
        b.setContentDescription(desc);
        int s = (int) (44 * getResources().getDisplayMetrics().density);
        int p = (int) (11 * getResources().getDisplayMetrics().density);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(s, s);
        lp.setMarginStart((int) (6 * getResources().getDisplayMetrics().density));
        b.setLayoutParams(lp);
        b.setPadding(p, p, p, p);
        b.setOnClickListener(l);
        toolbar.addView(b, 0);
        return b;
    }

    private void buildToolbar() {
        toolBtn(R.drawable.ic_close, getString(R.string.disconnected),
                v -> disconnectAndFinish());
        toolBtn(R.drawable.ic_keyboard, "key toolbar",
                v -> toggleView(keybarScroll));
        toolBtn(R.drawable.ic_ime, "soft keyboard", v -> {
            InputMethodManager imm = (InputMethodManager)
                    getSystemService(Context.INPUT_METHOD_SERVICE);
            streamView.requestFocus();
            if (imm != null) imm.showSoftInput(streamView, 0);
        });
        toolBtn(R.drawable.ic_mouse, "control mode", v -> showControlModePopup(v));
        toolBtn(R.drawable.ic_sliders, "quality", v -> showQualityPopup(v));
        toolBtn(R.drawable.ic_stats, "fps", v -> showFpsPopup(v));
        toolBtn(R.drawable.ic_fit, "scale mode", v -> {
            int m = streamView.getScaleMode() == StreamView.SCALE_FIT
                    ? StreamView.SCALE_ORIGINAL : StreamView.SCALE_FIT;
            streamView.setScaleMode(m);
            Toast.makeText(this, m == StreamView.SCALE_FIT
                    ? R.string.scale_fit : R.string.scale_original,
                    Toast.LENGTH_SHORT).show();
        });
        toolBtn(R.drawable.ic_fullscreen, "fullscreen", v -> setFullscreen(!fullscreen));
        toolBtn(R.drawable.ic_rotate, "orientation", v -> cycleOrientation());
        toolBtn(R.drawable.ic_display, "displays", v -> showDisplaysPopup(v));
        final ImageButton privacy = toolBtn(R.drawable.ic_eye_off, "privacy mode",
                v -> {
                    privacyOn = !privacyOn;
                    SystemActions.setPrivacy(client, privacyOn);
                    v.setAlpha(privacyOn ? 1f : 0.5f);
                    Toast.makeText(this, privacyOn
                            ? R.string.privacy_on : R.string.privacy_off,
                            Toast.LENGTH_SHORT).show();
                });
        privacy.setAlpha(0.5f);
        toolBtn(R.drawable.ic_bolt, "quick actions",
                v -> SystemActions.show(this, client));
        chatBtn = toolBtn(R.drawable.ic_chat, "chat", v -> {
            toggleView(chatDrawer);
            clipDrawer.setVisibility(View.GONE);
            if (chatDrawer.getVisibility() == View.VISIBLE) {
                chatUnread = 0;
                chatBtn.setAlpha(1f);
            }
        });
        toolBtn(R.drawable.ic_terminal, "terminal", v -> openTerminal(null));
        toolBtn(R.drawable.ic_gauge, "agent", v -> {
            Intent i = new Intent(this, AgentActivity.class);
            putConn(i);
            startActivity(i);
        });
        toolBtn(R.drawable.ic_info, "connection", v -> showConnectionPanel());
        toolBtn(R.drawable.ic_camera, "screenshot", v -> takeScreenshot());
        toolBtn(R.drawable.ic_record, "record", v -> toggleRecording());
        toolBtn(R.drawable.ic_videocam, "camera verification", v -> onCameraButton());
        toolBtn(R.drawable.ic_clipboard, "clipboard history", v -> {
            toggleView(clipDrawer);
            chatDrawer.setVisibility(View.GONE);
            refreshClipDrawer();
        });
        toolBtn(R.drawable.ic_users, "devices & permissions", v -> {
            Intent i = new Intent(this, UserManagementActivity.class);
            putConn(i);
            startActivity(i);
        });
        toolBtn(R.drawable.ic_folder, getString(R.string.files), v -> {
            Intent i = new Intent(this, FileManagerActivity.class);
            putConn(i);
            startActivity(i);
        });
    }

    private void putConn(Intent i) {
        i.putExtra(FileManagerActivity.EXTRA_HOST, host);
        i.putExtra(FileManagerActivity.EXTRA_PORT, port);
        i.putExtra(FileManagerActivity.EXTRA_PASSWORD, password);
    }

    private void toggleView(View v) {
        v.setVisibility(v.getVisibility() == View.VISIBLE ? View.GONE : View.VISIBLE);
    }

    private void openTerminal(String command) {
        Intent i = new Intent(this, TerminalActivity.class);
        putConn(i);
        if (command != null) i.putExtra(TerminalActivity.EXTRA_COMMAND, command);
        startActivity(i);
    }

    // ---- control mode / quality / fps popups --------------------------------

    private void showControlModePopup(View anchor) {
        PopupMenu menu = new PopupMenu(this, anchor);
        String[] names = {getString(R.string.mode_touch),
                getString(R.string.mode_trackpad), getString(R.string.mode_mouse)};
        for (int i = 0; i < names.length; i++) menu.getMenu().add(0, i, 0, names[i]);
        menu.getMenu().add(1, 10, 0, getString(R.string.precision)
                + (streamView.getPrecision() ? " ✓" : ""));
        menu.setOnMenuItemClickListener(item -> {
            int id = item.getItemId();
            if (id == 10) {
                streamView.setPrecision(!streamView.getPrecision());
            } else {
                streamView.setControlMode(id);
                updateMouseBar();
                Toast.makeText(this, names[id], Toast.LENGTH_SHORT).show();
            }
            return true;
        });
        menu.show();
    }

    private void updateMouseBar() {
        mouseBar.setVisibility(streamView.getControlMode() == StreamView.MODE_MOUSE
                ? View.VISIBLE : View.GONE);
    }

    private void showQualityPopup(View anchor) {
        PopupMenu menu = new PopupMenu(this, anchor);
        String cur = Prefs.getString(this, Prefs.K_QUALITY, "balanced");
        for (int i = 0; i < QUALITIES.length; i++) {
            String label = qualityLabel(QUALITIES[i])
                    + (QUALITIES[i].equals(cur) ? " ✓" : "");
            menu.getMenu().add(0, i, 0, label);
        }
        menu.getMenu().add(1, 10, 0, getString(R.string.adaptive)
                + (Prefs.getBool(this, Prefs.K_ADAPTIVE, true) ? " ✓" : ""));
        menu.setOnMenuItemClickListener(item -> {
            int id = item.getItemId();
            if (id == 10) {
                boolean on = !Prefs.getBool(this, Prefs.K_ADAPTIVE, true);
                Prefs.putBool(this, Prefs.K_ADAPTIVE, on);
                if (!on && adaptiveReduced) restoreAdaptive();
            } else {
                applyQuality(QUALITIES[id], true);
            }
            return true;
        });
        menu.show();
    }

    private String qualityLabel(String q) {
        if ("ultra".equals(q)) return getString(R.string.q_ultra);
        if ("high".equals(q)) return getString(R.string.q_high);
        if ("balanced".equals(q)) return getString(R.string.q_balanced);
        if ("low".equals(q)) return getString(R.string.q_low);
        return getString(R.string.q_minlat);
    }

    private void applyQuality(String q, boolean toast) {
        int idx = 2;
        for (int i = 0; i < QUALITIES.length; i++) {
            if (QUALITIES[i].equals(q)) idx = i;
        }
        Prefs cur = Prefs.load(this);
        Prefs.save(this, cur.haptics, cur.keepAwake, QUALITY_SMOOTH[idx],
                cur.clipboard, QUALITY_FPS[idx]);
        Prefs.putString(this, Prefs.K_QUALITY, QUALITIES[idx]);
        prefs = Prefs.load(this);
        streamView.applyPrefs(prefs);
        streamView.setDecodeSampleSize(QUALITY_SAMPLE[idx]);
        adaptiveReduced = false;
        adaptiveBadge.setVisibility(View.GONE);
        if (toast) Toast.makeText(this, qualityLabel(QUALITIES[idx]),
                Toast.LENGTH_SHORT).show();
    }

    private void showFpsPopup(View anchor) {
        PopupMenu menu = new PopupMenu(this, anchor);
        int[] vals = {15, 30, 45, 60};
        for (int v : vals) menu.getMenu().add(0, v, 0, v + " fps");
        menu.setOnMenuItemClickListener(item -> {
            int cap = item.getItemId();
            Prefs cur = Prefs.load(this);
            Prefs.save(this, cur.haptics, cur.keepAwake, cur.smoothScaling,
                    cur.clipboard, cap);
            prefs = Prefs.load(this);
            streamView.applyPrefs(prefs);
            Toast.makeText(this, cap + " fps", Toast.LENGTH_SHORT).show();
            return true;
        });
        menu.show();
    }

    // ---- fullscreen / orientation -------------------------------------------

    private void setFullscreen(boolean on) {
        fullscreen = on;
        toolbarScroll.setVisibility(on ? View.GONE : View.VISIBLE);
        btnExitFs.setVisibility(on ? View.VISIBLE : View.GONE);
        int vis = on
                ? View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                | View.SYSTEM_UI_FLAG_FULLSCREEN
                | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                | View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
                : View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION;
        getWindow().getDecorView().setSystemUiVisibility(vis);
    }

    private void cycleOrientation() {
        orientationIdx = (orientationIdx + 1) % 3;
        int mode = orientationIdx == 1
                ? ActivityInfo.SCREEN_ORIENTATION_LANDSCAPE
                : orientationIdx == 2
                ? ActivityInfo.SCREEN_ORIENTATION_PORTRAIT
                : ActivityInfo.SCREEN_ORIENTATION_SENSOR;
        setRequestedOrientation(mode);
        String[] names = {getString(R.string.orient_sensor),
                getString(R.string.orient_landscape), getString(R.string.orient_portrait)};
        Toast.makeText(this, names[orientationIdx], Toast.LENGTH_SHORT).show();
    }

    // ---- displays / connection panel ----------------------------------------

    private void showDisplaysPopup(View anchor) {
        if (client == null || client.getProtoVersion() < 3) {
            Toast.makeText(this, R.string.displays_unsupported,
                    Toast.LENGTH_SHORT).show();
            return;
        }
        if (displaysJson == null) {
            client.sendDisplaysQuery();
            Toast.makeText(this, R.string.displays_loading,
                    Toast.LENGTH_SHORT).show();
            return;
        }
        try {
            JSONObject o = new JSONObject(displaysJson);
            JSONArray arr = o.optJSONArray("displays");
            if (arr == null || arr.length() == 0) {
                Toast.makeText(this, R.string.displays_none,
                        Toast.LENGTH_SHORT).show();
                return;
            }
            PopupMenu menu = new PopupMenu(this, anchor);
            final List<String> ids = new ArrayList<String>();
            for (int i = 0; i < arr.length(); i++) {
                JSONObject d = arr.getJSONObject(i);
                ids.add(d.optString("id", String.valueOf(i)));
                String label = d.optString("name", "Display " + (i + 1))
                        + " " + d.optInt("w", 0) + "x" + d.optInt("h", 0)
                        + (d.optBoolean("primary", false) ? " ★" : "");
                menu.getMenu().add(0, i, 0, label);
            }
            menu.setOnMenuItemClickListener(item -> {
                SystemActions.switchDisplay(client, ids.get(item.getItemId()));
                return true;
            });
            menu.show();
        } catch (Exception e) {
            Toast.makeText(this, R.string.displays_unsupported,
                    Toast.LENGTH_SHORT).show();
        }
    }

    private void showConnectionPanel() {
        RemoteClient c = client;
        String latency = "—";
        String state = getString(R.string.conn_reconnecting);
        String transport = getString(R.string.transport_tailscale);
        String endpoint = host + ":" + port;
        String proto = "v" + (c == null ? 1 : c.getProtoVersion());
        String bytes = "";
        if (connected && c != null) {
            state = getString(R.string.conn_connected);
            bytes = "↓ " + humanBytes(c.getRxBytes())
                    + "  ↑ " + humanBytes(c.getTxBytes());
        }
        // latest measured RTT is shown live in the stats line; repeat it here
        String msg = getString(R.string.conn_panel, transport, endpoint,
                state, lastRttText, proto, bytes);
        new AlertDialog.Builder(this)
                .setTitle(R.string.conn_title)
                .setMessage(msg)
                .setPositiveButton(R.string.conn_reconnect_now, (d, w) -> reconnectNow())
                .setNegativeButton(R.string.cancel, null)
                .show();
    }

    private String lastRttText = "—";

    private void reconnectNow() {
        if (client != null) {
            client.stop();
            client = null;
        }
        connected = false;
        showStatus(getString(R.string.reconnecting), false);
        client = new RemoteClient(this, host, port, password, this);
        client.start();
        Toast.makeText(this, R.string.conn_reconnecting, Toast.LENGTH_SHORT).show();
    }

    private static String humanBytes(long b) {
        if (b < 1024) return b + " B";
        double kb = b / 1024.0;
        if (kb < 1024) return String.format(Locale.US, "%.0f KB", kb);
        double mb = kb / 1024.0;
        if (mb < 1024) return String.format(Locale.US, "%.1f MB", mb);
        return String.format(Locale.US, "%.2f GB", mb / 1024.0);
    }

    // ---- screenshots --------------------------------------------------------

    private void takeScreenshot() {
        final Bitmap bmp = streamView.snapshotCopy();
        if (bmp == null) {
            Toast.makeText(this, R.string.shot_no_frame, Toast.LENGTH_SHORT).show();
            return;
        }
        new Thread(() -> {
            try {
                Screenshots.capture(SessionActivity.this, bmp,
                        new Screenshots.Callback() {
                            @Override public void onSaved(final Uri uri) {
                                ui.post(() -> {
                                    saveShotHistory(uri.toString());
                                    Toast.makeText(SessionActivity.this,
                                            R.string.shot_saved, Toast.LENGTH_SHORT).show();
                                    new AlertDialog.Builder(SessionActivity.this)
                                            .setMessage(R.string.shot_saved)
                                            .setPositiveButton(R.string.share,
                                                    (d, w) -> Screenshots.share(
                                                            SessionActivity.this, uri))
                                            .setNegativeButton(R.string.cancel, null)
                                            .show();
                                });
                            }
                            @Override public void onError(final String reason) {
                                ui.post(() -> Toast.makeText(SessionActivity.this,
                                        reason, Toast.LENGTH_LONG).show());
                            }
                        });
            } finally {
                bmp.recycle();
            }
        }).start();
    }

    private static final String KEY_SHOTS = "shot_history";

    private void saveShotHistory(String uri) {
        try {
            JSONArray arr = new JSONArray(Prefs.getString(this, KEY_SHOTS, "[]"));
            JSONArray out = new JSONArray();
            out.put(uri);
            for (int i = 0; i < arr.length() && i < 19; i++) out.put(arr.get(i));
            Prefs.putString(this, KEY_SHOTS, out.toString());
        } catch (Exception ignored) {
        }
    }

    // ---- recording ----------------------------------------------------------

    private void toggleRecording() {
        if (recorder == null) {
            startRecording();
        } else if (recorder.isPaused()) {
            recorder.resume();
            // shift the start time so the paused gap is not counted
            recStartMs += System.currentTimeMillis() - pauseStartMs;
            pauseStartMs = 0;
            recTime.setText(R.string.recording);
            Toast.makeText(this, R.string.rec_resumed, Toast.LENGTH_SHORT).show();
        } else {
            // confirm pause vs stop
            new AlertDialog.Builder(this)
                    .setTitle(R.string.recording)
                    .setItems(new CharSequence[]{getString(R.string.pause),
                                    getString(R.string.stop)},
                            (d, which) -> {
                                if (which == 0) {
                                    recorder.pause();
                                    pauseStartMs = System.currentTimeMillis();
                                    recTime.setText(R.string.paused);
                                } else {
                                    stopRecording();
                                }
                            })
                    .show();
        }
    }

    private void startRecording() {
        try {
            File dir = new File(getExternalFilesDir(
                    android.os.Environment.DIRECTORY_MOVIES), "Remote");
            if (!dir.exists()) dir.mkdirs();
            File tmp = new File(dir, "rec_" + System.currentTimeMillis() + ".mp4");
            recorder = new ScreenRecorder(tmp);
            recorder.start(1280, 720, 15);
            recStartMs = System.currentTimeMillis();
            pauseStartMs = 0;
            recIndicator.setVisibility(View.VISIBLE);
            recTime.setText(R.string.recording);
            ui.post(recTicker);
            Toast.makeText(this, R.string.rec_started, Toast.LENGTH_SHORT).show();
        } catch (Exception e) {
            recorder = null;
            String m = e.getMessage();
            Toast.makeText(this, m == null ? "recording failed" : m,
                    Toast.LENGTH_LONG).show();
        }
    }

    private void stopRecording() {
        final ScreenRecorder r = recorder;
        recorder = null;
        ui.removeCallbacks(recTicker);
        recIndicator.setVisibility(View.GONE);
        if (r == null) return;
        new Thread(() -> {
            final File f = r.stop();
            // publish to the gallery (MediaStore) so it survives uninstalls
            final Uri uri = publishVideo(f);
            // On API < 29 the returned URI IS the file itself — never delete it there.
            if (Build.VERSION.SDK_INT >= 29 && f != null && f.exists()) f.delete();
            ui.post(() -> {
                if (uri != null) {
                    saveShotHistory(uri.toString());
                    Toast.makeText(SessionActivity.this,
                            R.string.rec_saved, Toast.LENGTH_SHORT).show();
                } else {
                    Toast.makeText(SessionActivity.this,
                            R.string.rec_failed, Toast.LENGTH_LONG).show();
                }
            });
        }).start();
    }

    private Uri publishVideo(File f) {
        if (f == null || !f.exists()) return null;
        try {
            if (Build.VERSION.SDK_INT >= 29) {
                android.content.ContentValues v = new android.content.ContentValues();
                String name = f.getName();
                v.put(android.provider.MediaStore.Video.Media.DISPLAY_NAME, name);
                v.put(android.provider.MediaStore.Video.Media.MIME_TYPE, "video/mp4");
                v.put(android.provider.MediaStore.Video.Media.RELATIVE_PATH,
                        android.os.Environment.DIRECTORY_MOVIES + "/Remote");
                Uri uri = getContentResolver().insert(
                        android.provider.MediaStore.Video.Media.EXTERNAL_CONTENT_URI, v);
                if (uri == null) return null;
                java.io.OutputStream os = getContentResolver().openOutputStream(uri);
                java.io.InputStream in = new java.io.FileInputStream(f);
                try {
                    byte[] buf = new byte[65536];
                    int n;
                    while ((n = in.read(buf)) > 0) os.write(buf, 0, n);
                } finally {
                    try { in.close(); } catch (Exception ignored) {}
                    try { os.close(); } catch (Exception ignored) {}
                }
                return uri;
            } else {
                return Uri.fromFile(f); // keep the private file on old Android
            }
        } catch (Exception e) {
            return null;
        }
    }

    // ---- camera for verification (v4) -----------------------------------------

    private void onCameraButton() {
        if (cameraStreamer.isStreaming() || cameraPendingStart) {
            showCameraPanel();
            return;
        }
        RemoteClient c = client;
        if (c == null || !connected || c.getProtoVersion() < 4) {
            Toast.makeText(this, R.string.cam_needs_v4, Toast.LENGTH_LONG).show();
            return;
        }
        if (checkSelfPermission(Manifest.permission.CAMERA)
                == PackageManager.PERMISSION_GRANTED) {
            showCameraPanel();
        } else {
            requestPermissions(new String[]{Manifest.permission.CAMERA},
                    REQ_CAMERA_PERM);
        }
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions,
                                           int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode != REQ_CAMERA_PERM) return;
        boolean granted = grantResults.length > 0
                && grantResults[0] == PackageManager.PERMISSION_GRANTED;
        if (granted) {
            showCameraPanel();
        } else if (!shouldShowRequestPermissionRationale(
                Manifest.permission.CAMERA)) {
            // permanently denied: offer app settings once, never nag
            new AlertDialog.Builder(this)
                    .setTitle(R.string.cam_perm_title)
                    .setMessage(R.string.cam_perm_permanent)
                    .setPositiveButton(R.string.cam_open_settings, (d, w) -> {
                        Intent i = new Intent(
                                android.provider.Settings
                                        .ACTION_APPLICATION_DETAILS_SETTINGS,
                                Uri.fromParts("package", getPackageName(), null));
                        startActivity(i);
                    })
                    .setNegativeButton(R.string.cancel, null)
                    .show();
        } else {
            Toast.makeText(this, R.string.cam_perm_denied, Toast.LENGTH_LONG).show();
        }
    }

    private void showCameraPanel() {
        if (cameraStreamer.isStreaming()) {
            String info = getString(R.string.cam_active_info,
                    "front".equals(cameraStreamer.getFacing())
                            ? getString(R.string.cam_front)
                            : getString(R.string.cam_rear),
                    cameraStreamer.getWidth(), cameraStreamer.getHeight(),
                    cameraStreamer.getFps());
            new AlertDialog.Builder(this)
                    .setTitle(R.string.cam_active_title)
                    .setMessage(info)
                    .setPositiveButton(R.string.cam_stop,
                            (d, w) -> stopCamera(null))
                    .setNeutralButton(R.string.cam_switch,
                            (d, w) -> switchCamera())
                    .setNegativeButton(R.string.cancel, null)
                    .show();
            return;
        }
        if (cameraPendingStart) {
            Toast.makeText(this, R.string.cam_starting, Toast.LENGTH_SHORT).show();
            return;
        }
        final String[] facings = {"rear", "front"};
        final String[] labels = {getString(R.string.cam_rear),
                getString(R.string.cam_front)};
        final int[] sel = {0};
        if (CameraStreamer.findCamera(this, "rear") == null
                && CameraStreamer.findCamera(this, "front") != null) {
            sel[0] = 1;
        }
        new AlertDialog.Builder(this)
                .setTitle(R.string.cam_title)
                .setMessage(R.string.cam_explain)
                .setSingleChoiceItems(labels, sel[0], (d, which) -> sel[0] = which)
                .setPositiveButton(R.string.cam_start,
                        (d, w) -> startCameraFlow(facings[sel[0]]))
                .setNegativeButton(R.string.cancel, null)
                .show();
    }

    /**
     * Explicit user tap is the ONLY trigger. Resolves the camera
     * capabilities, sends CAMERA_START, and waits for the host's
     * CAMERA_STATUS before capturing a single frame.
     */
    private void startCameraFlow(String facing) {
        RemoteClient c = client;
        if (c == null || !connected || c.getProtoVersion() < 4) {
            Toast.makeText(this, R.string.cam_needs_v4, Toast.LENGTH_LONG).show();
            return;
        }
        if (checkSelfPermission(Manifest.permission.CAMERA)
                != PackageManager.PERMISSION_GRANTED) {
            Toast.makeText(this, R.string.cam_perm_denied, Toast.LENGTH_LONG).show();
            return;
        }
        try {
            CameraStreamer.CameraInfo info = CameraStreamer.findCamera(this, facing);
            if (info == null) {
                Toast.makeText(this, getString(R.string.cam_no_camera, facing),
                        Toast.LENGTH_LONG).show();
                return;
            }
            int[] wh = CameraStreamer.chooseSize(this, info.id);
            int fps = CameraStreamer.chooseFps(this, info.id);
            int rotation = CameraStreamer.computeRotation(this, info);
            camPendW = wh[0];
            camPendH = wh[1];
            camPendFps = fps;
            camPendFacing = facing;
            camPendRotation = rotation;
            camPendMirror = "front".equals(facing);
        } catch (Exception e) {
            String m = e.getMessage();
            Toast.makeText(this, getString(R.string.cam_init_failed,
                    m == null ? "?" : m), Toast.LENGTH_LONG).show();
            return;
        }
        cameraPendingStart = true;
        updateCamIndicator();
        c.sendCameraStart(camPendW, camPendH, camPendFps, camPendFacing,
                camPendRotation, camPendMirror);
        ui.postDelayed(cameraStartTimeout, 10000);
        Toast.makeText(this, R.string.cam_starting, Toast.LENGTH_SHORT).show();
    }

    private final Runnable cameraStartTimeout = new Runnable() {
        @Override public void run() {
            if (cameraPendingStart) {
                cameraPendingStart = false;
                updateCamIndicator();
                Toast.makeText(SessionActivity.this, R.string.cam_no_response,
                        Toast.LENGTH_LONG).show();
            }
        }
    };

    /** Host accepted: begin local capture and stream access units. */
    private void beginCapture() {
        camQueue.clear();
        camDropped.set(0);
        camSending.set(true);
        camSenderThread = new Thread(new Runnable() {
            @Override public void run() {
                try {
                    while (camSending.get() || !camQueue.isEmpty()) {
                        byte[] au = camQueue.poll(200, TimeUnit.MILLISECONDS);
                        RemoteClient c = client;
                        if (au != null && c != null) c.sendCameraFrame(au);
                    }
                } catch (InterruptedException ignored) {
                }
            }
        }, "CamNetSender");
        camSenderThread.setDaemon(true);
        camSenderThread.start();

        cameraStreamer.start(this, camPendFacing,
                new CameraStreamer.FrameSink() {
                    @Override public void onAccessUnit(byte[] data) {
                        // bounded handoff: drop-oldest, memory never grows
                        if (!camQueue.offer(data)) {
                            camQueue.poll();
                            camQueue.offer(data);
                            camDropped.incrementAndGet();
                        }
                    }
                },
                new CameraStreamer.Callback() {
                    @Override public void onStarted(int w, int h, int fps,
                                                   String facing) {
                        ui.post(() -> {
                            updateCamIndicator();
                            Toast.makeText(SessionActivity.this,
                                    R.string.cam_streaming, Toast.LENGTH_SHORT)
                                    .show();
                        });
                    }
                    @Override public void onError(final String reason) {
                        ui.post(() -> stopCamera(
                                getString(R.string.cam_error, reason)));
                    }
                    @Override public void onStopped() {
                        ui.post(() -> updateCamIndicator());
                    }
                });
    }

    /**
     * Stops everything: local capture first (no more frames are produced),
     * then the sender, then CAMERA_STOP to the host. Never auto-restarts.
     */
    private void stopCamera(String reason) {
        cameraPendingStart = false;
        ui.removeCallbacks(cameraStartTimeout);
        camSending.set(false);
        Thread t = camSenderThread;
        camSenderThread = null;
        if (t != null) t.interrupt(); // daemon: exits on interrupt
        camQueue.clear();
        if (cameraStreamer != null) cameraStreamer.stop();
        RemoteClient c = client;
        if (c != null) c.sendCameraStop();
        updateCamIndicator();
        if (reason != null) {
            Toast.makeText(this, reason, Toast.LENGTH_LONG).show();
        }
    }

    private void switchCamera() {
        String other = "front".equals(camPendFacing) ? "rear" : "front";
        stopCamera(null);
        ui.postDelayed(() -> startCameraFlow(other), 400);
    }

    private void updateCamIndicator() {
        ui.post(() -> {
            if (cameraStreamer != null && cameraStreamer.isStreaming()) {
                camStatusText.setText(getString(R.string.cam_indicator_active,
                        "front".equals(cameraStreamer.getFacing())
                                ? getString(R.string.cam_front)
                                : getString(R.string.cam_rear)));
                camIndicator.setVisibility(View.VISIBLE);
            } else if (cameraPendingStart) {
                camStatusText.setText(R.string.cam_indicator_starting);
                camIndicator.setVisibility(View.VISIBLE);
            } else {
                camIndicator.setVisibility(View.GONE);
            }
        });
    }

    @Override
    public void onCameraStatus(final String json) {
        ui.post(() -> {
            CameraProtocol.Status st;
            try {
                st = CameraProtocol.parseStatus(json);
            } catch (IllegalArgumentException e) {
                return; // malformed status: ignore, never tear down on garbage
            }
            if (cameraPendingStart) {
                cameraPendingStart = false;
                ui.removeCallbacks(cameraStartTimeout);
                if (st.active) {
                    beginCapture();
                } else {
                    updateCamIndicator();
                    Toast.makeText(SessionActivity.this,
                            getString(R.string.cam_error,
                                    st.error == null ? "?" : st.error),
                            Toast.LENGTH_LONG).show();
                }
            } else if (cameraStreamer.isStreaming() && !st.active) {
                // host stopped or failed mid-stream (incl. permission revoked)
                stopCamera(st.error == null ? null
                        : getString(R.string.cam_error, st.error));
            }
        });
    }

    @Override
    public void onCameraStopReceived() {
        ui.post(() -> {
            if (cameraStreamer.isStreaming() || cameraPendingStart) {
                stopCamera(getString(R.string.cam_stopped_by_host));
            }
        });
    }

    // ---- keyboard toolbar ---------------------------------------------------

    private Button keyBtn(String label, View.OnClickListener l) {
        Button b = new Button(this, null, 0, R.style.RemoteKeyButton);
        b.setText(label);
        b.setOnClickListener(l);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.WRAP_CONTENT);
        int m = (int) (3 * getResources().getDisplayMetrics().density);
        lp.setMargins(m, m, m, m);
        b.setLayoutParams(lp);
        keybar.addView(b);
        return b;
    }

    private void buildKeybar() {
        // sticky modifiers
        String[] mods = {"Ctrl", "Alt", "Shift", "Super"};
        final String[] modKeys = {"Control_L", "Alt_L", "Shift_L", "Super_L"};
        for (int i = 0; i < mods.length; i++) {
            final String key = modKeys[i];
            final Button b = keyBtn(mods[i], v -> {
                if (stickyMods.contains(key)) stickyMods.remove(key);
                else stickyMods.add(key);
                refreshModButtons();
            });
            b.setTag(key);
            modButtons.add(b);
        }
        refreshModButtons();
        // common keys
        String[] keys = {"Esc", "Tab", "Del", "Home", "End", "PgUp", "PgDn",
                "Up", "Down", "Left", "Right"};
        final String[] keyNames = {"Escape", "Tab", "Delete", "Home", "End",
                "Page_Up", "Page_Down", "Up", "Down", "Left", "Right"};
        for (int i = 0; i < keys.length; i++) {
            final String kn = keyNames[i];
            keyBtn(keys[i], v -> sendKeyWithMods(kn));
        }
        // F1-F12
        for (int f = 1; f <= 12; f++) {
            final String kn = "F" + f;
            keyBtn(kn, v -> sendKeyWithMods(kn));
        }
        // combos
        String[] combos = {"Ctrl+C", "Ctrl+V", "Ctrl+X", "Ctrl+Z", "Ctrl+A"};
        final String[] comboKeys = {"c", "v", "x", "z", "a"};
        for (int i = 0; i < combos.length; i++) {
            final String kn = comboKeys[i];
            keyBtn(combos[i], v -> {
                stickyMods.add("Control_L");
                sendKeyWithMods(kn);
            });
        }
        // custom shortcut bar
        refreshCustomShortcuts();
        keyBtn("✎", v -> editShortcuts());
        // linux quick buttons
        keyBtn("Term", v -> {
            if (client != null) client.sendSystemCmd(SystemActions.OPEN_TERMINAL, null);
        });
        keyBtn("Lock", v -> {
            if (client != null) client.sendSystemCmd(SystemActions.LOCK, null);
        });
    }

    private void refreshModButtons() {
        for (Button b : modButtons) {
            String key = (String) b.getTag();
            boolean on = stickyMods.contains(key);
            b.setAlpha(on ? 1f : 0.55f);
        }
    }

    private void refreshCustomShortcuts() {
        // custom shortcut buttons are rebuilt on edit; simplest: they live
        // at the end before the edit button — track and rebuild them.
        // (Rebuilds the whole bar section: remove all views tagged "custom".)
        for (int i = keybar.getChildCount() - 1; i >= 0; i--) {
            View v = keybar.getChildAt(i);
            if ("custom".equals(v.getTag())) keybar.removeViewAt(i);
        }
        try {
            JSONArray arr = new JSONArray(
                    Prefs.getString(this, Prefs.K_SHORTCUTS, defaultShortcuts()));
            for (int i = 0; i < arr.length(); i++) {
                final String kn = arr.optString(i, "").trim();
                if (kn.isEmpty()) continue;
                Button b = keyBtn(shortLabel(kn), v -> sendKeyWithMods(kn));
                b.setTag("custom");
                // keep custom buttons grouped: move before the edit + quick buttons
                keybar.removeView(b);
                keybar.addView(b, keybar.getChildCount() - 3);
            }
        } catch (Exception ignored) {
        }
    }

    private static String defaultShortcuts() {
        return "[\"F5\",\"F11\",\"Print\"]";
    }

    private static String shortLabel(String keyName) {
        return keyName.replace("_L", "").replace("_R", "");
    }

    private void editShortcuts() {
        final EditText f = new EditText(this);
        f.setHint("F5, F11, Escape");
        try {
            JSONArray arr = new JSONArray(
                    Prefs.getString(this, Prefs.K_SHORTCUTS, defaultShortcuts()));
            StringBuilder sb = new StringBuilder();
            for (int i = 0; i < arr.length(); i++) {
                if (i > 0) sb.append(", ");
                sb.append(arr.optString(i, ""));
            }
            f.setText(sb.toString());
        } catch (Exception ignored) {
        }
        f.setSingleLine(false);
        int pad = (int) (20 * getResources().getDisplayMetrics().density);
        f.setPadding(pad, pad / 2, pad, pad / 2);
        new AlertDialog.Builder(this)
                .setTitle(R.string.shortcuts_title)
                .setMessage(R.string.shortcuts_hint)
                .setView(f)
                .setPositiveButton(R.string.save, (d, w) -> {
                    try {
                        JSONArray arr = new JSONArray();
                        for (String part : f.getText().toString().split(",")) {
                            String t = part.trim();
                            if (!t.isEmpty()) arr.put(t);
                        }
                        Prefs.putString(this, Prefs.K_SHORTCUTS, arr.toString());
                        refreshCustomShortcuts();
                    } catch (Exception ignored) {
                    }
                })
                .setNegativeButton(R.string.cancel, null)
                .show();
    }

    private void sendKeyWithMods(String keyName) {
        for (String m : stickyMods) onKey(m, true);
        onKey(keyName, true);
        onKey(keyName, false);
        for (String m : stickyMods) onKey(m, false);
        stickyMods.clear();
        refreshModButtons();
    }

    // ---- chat ---------------------------------------------------------------

    private void sendChat() {
        String text = chatInput.getText().toString().trim();
        if (text.isEmpty() || client == null) return;
        client.sendChat(Build.MODEL, text);
        chatInput.setText("");
    }

    private void addChatRow(String from, String text, long ts) {
        String when = new SimpleDateFormat("HH:mm", Locale.US).format(new Date(ts));
        chatRows.add("[" + when + "] " + from + ": " + text);
        while (chatRows.size() > 200) chatRows.remove(0);
        chatAdapter.notifyDataSetChanged();
        chatList.setSelection(chatRows.size() - 1);
    }

    // ---- clipboard history drawer ---------------------------------------------

    private void refreshClipDrawer() {
        clipRows.clear();
        List<String> items = ClipboardHistory.list(this);
        for (String s : items) {
            String one = s.replace('\n', ' ').trim();
            if (one.length() > 80) one = one.substring(0, 80) + "…";
            clipRows.add(one);
        }
        clipAdapter.notifyDataSetChanged();
    }

    // ---- status ---------------------------------------------------------------

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
        if (cameraStreamer.isStreaming() || cameraPendingStart) stopCamera(null);
        if (recorder != null) stopRecording();
        if (client != null) {
            client.sendDisconnect();
            client.stop();
            client = null;
        }
        finish();
    }

    private void pokeTimeout() {
        int timeoutMin = Prefs.getInt(this, Prefs.K_SESSION_TIMEOUT_MIN, 0);
        if (timeoutMin > 0) {
            ui.removeCallbacks(timeoutFire);
            ui.postDelayed(timeoutFire, timeoutMin * 60L * 1000L);
        }
    }

    private void saveHostMeta(String resolution) {
        try {
            JSONObject all = new JSONObject(
                    Prefs.getString(this, Prefs.K_HOST_META, "{}"));
            JSONObject m = new JSONObject();
            m.put("resolution", resolution);
            m.put("last_seen", System.currentTimeMillis());
            all.put(host + ":" + port, m);
            Prefs.putString(this, Prefs.K_HOST_META, all.toString());
        } catch (Exception ignored) {
        }
    }

    // ---- RemoteClient.Listener --------------------------------------------------

    @Override
    public void onConnected() {
        connected = true;
        hideStatus();
        rttCount = 0;
        rttGoodStreak = 0;
        if (adaptiveReduced) restoreAdaptive();
        sessionDbId = SessionDb.get(this).startSession(
                deviceLabel, host + ":" + port, System.currentTimeMillis());
        SecurityCenterActivity.markTrusted(this, host + ":" + port);
        Notify.sessionConnected(this, deviceLabel);
        RemoteClient c = client;
        if (c != null) c.sendDisplaysQuery();
        ui.post(() -> pokeTimeout());
    }

    @Override
    public void onProtoVersion(int version) {
        // v1/v2 hosts: v3 buttons degrade — toasts explain per action.
    }

    @Override
    public void onAuthFailed(final String reason) {
        SessionDb.get(this).logEvent("auth-failed", host + ":" + port);
        ui.post(() -> {
            Toast.makeText(SessionActivity.this,
                    getString(R.string.auth_failed), Toast.LENGTH_LONG).show();
            finish();
        });
    }

    @Override
    public void onDisconnected(final boolean willRetry, final String reason) {
        // Privacy: the camera NEVER survives a disconnect and never
        // auto-resumes. The user must explicitly tap Start Camera again.
        if (cameraStreamer.isStreaming() || cameraPendingStart) {
            stopCamera(null);
        }
        if (willRetry) {
            connected = false;
            showStatus(getString(R.string.reconnecting), true);
        } else {
            endSessionRecord();
            hideStatus();
        }
    }

    private void endSessionRecord() {
        if (sessionDbId >= 0) {
            RemoteClient c = client;
            SessionDb.get(this).endSession(sessionDbId, System.currentTimeMillis(),
                    c == null ? 0 : c.getRxBytes(), c == null ? 0 : c.getTxBytes());
            sessionDbId = -1;
        }
        if (connected) {
            Notify.sessionDisconnected(this, deviceLabel, null);
        }
        connected = false;
    }

    @Override
    public void onFrame(byte[] jpeg) {
        streamView.submitFrame(jpeg);
    }

    @Override
    public void onStats(final int fps, final long rttMs) {
        ui.post(() -> {
            lastRttText = rttMs < 0 ? "—" : rttMs + " ms";
            stats.setText(fps + " fps · " + lastRttText);
            adaptiveTick(rttMs);
        });
    }

    /** Adaptive quality: sustained PONG latency >300ms drops render scale. */
    private void adaptiveTick(long rttMs) {
        if (!Prefs.getBool(this, Prefs.K_ADAPTIVE, true) || rttMs < 0) return;
        rttWin[rttCount % rttWin.length] = rttMs;
        rttCount++;
        if (rttCount >= rttWin.length) {
            boolean bad = true;
            for (long r : rttWin) if (r <= 300) bad = false;
            if (bad && !adaptiveReduced) {
                adaptiveReduced = true;
                streamView.setDecodeSampleSize(2);
                // Silent and once: the banner appears quietly the first time
                // quality drops, then never pops again this session.
                if (!adaptiveBadgeShown) {
                    adaptiveBadgeShown = true;
                    adaptiveBadge.setText(R.string.adaptive_reduced);
                    adaptiveBadge.setVisibility(View.VISIBLE);
                }
                rttGoodStreak = 0;
            } else if (bad) {
                rttGoodStreak = 0;
            } else if (adaptiveReduced) {
                boolean good = true;
                for (long r : rttWin) if (r >= 150) good = false;
                if (good && ++rttGoodStreak >= 5) restoreAdaptive();
                else if (!good) rttGoodStreak = 0;
            }
        }
    }

    private void restoreAdaptive() {
        adaptiveReduced = false;
        rttGoodStreak = 0;
        // restore the preset's sample size
        String q = Prefs.getString(this, Prefs.K_QUALITY, "balanced");
        int idx = 2;
        for (int i = 0; i < QUALITIES.length; i++) {
            if (QUALITIES[i].equals(q)) idx = i;
        }
        streamView.setDecodeSampleSize(QUALITY_SAMPLE[idx]);
        adaptiveBadge.setVisibility(View.GONE);
    }

    @Override
    public void onClipboardText(String text) {
        if (clipboardSync != null) clipboardSync.applyRemoteText(text);
        ClipboardHistory.add(this, text);
        if (clipDrawer.getVisibility() == View.VISIBLE) refreshClipDrawer();
    }

    @Override
    public void onFileMsg(int type, byte[] payload) {
        // The session's own connection carries no file ops; FileManagerActivity
        // opens its own connection. Ignore.
    }

    @Override
    public void onSystemResp(final String cmd, final boolean ok, final String detail) {
        ui.post(() -> Toast.makeText(this,
                SystemActions.formatResp(cmd, ok, detail),
                Toast.LENGTH_LONG).show());
    }

    @Override
    public void onPermsDenied(final String op, final String reason) {
        ui.post(() -> Toast.makeText(this,
                getString(R.string.perms_denied, reason),
                Toast.LENGTH_LONG).show());
    }

    @Override
    public void onChat(final String from, final String text, final long ts) {
        ui.post(() -> {
            addChatRow(from.isEmpty() ? "host" : from, text, ts);
            if (chatDrawer.getVisibility() != View.VISIBLE) {
                chatUnread++;
                if (chatBtn != null) chatBtn.setAlpha(0.45f);
                Toast.makeText(this,
                        getString(R.string.chat_preview,
                                from.isEmpty() ? "host" : from, text),
                        Toast.LENGTH_SHORT).show();
            }
        });
    }

    @Override
    public void onDisplays(final String json) {
        displaysJson = json;
        // harvest resolution for the device list
        try {
            JSONObject o = new JSONObject(json);
            String active = o.optString("active", "");
            JSONArray arr = o.optJSONArray("displays");
            if (arr != null) {
                for (int i = 0; i < arr.length(); i++) {
                    JSONObject d = arr.getJSONObject(i);
                    if (active.equals(d.optString("id", ""))
                            || (active.isEmpty() && d.optBoolean("primary", false))
                            || (active.isEmpty() && i == 0)) {
                        saveHostMeta(d.optInt("w", 0) + "x" + d.optInt("h", 0));
                        break;
                    }
                }
            }
        } catch (Exception ignored) {
        }
    }

    // ---- StreamView.InputListener -> Remote INPUT events -------------------------

    private void send(String json) {
        pokeTimeout();
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
    protected void onResume() {
        super.onResume();
        // Permission revoked while away (e.g. one-time grant expired):
        // stop immediately, never keep streaming without the grant.
        if (cameraStreamer.isStreaming()
                && checkSelfPermission(Manifest.permission.CAMERA)
                != PackageManager.PERMISSION_GRANTED) {
            stopCamera(getString(R.string.cam_perm_revoked));
        }
    }

    @Override
    protected void onDestroy() {
        ui.removeCallbacks(timeoutFire);
        ui.removeCallbacks(recTicker);
        ui.removeCallbacks(cameraStartTimeout);
        if (cameraStreamer.isStreaming() || cameraPendingStart) stopCamera(null);
        if (recorder != null) {
            try { recorder.stop(); } catch (Exception ignored) {}
            recorder = null;
        }
        endSessionRecord();
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
