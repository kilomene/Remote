package com.remote.viewer;

import android.app.Activity;
import android.content.Context;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.view.LayoutInflater;
import android.view.View;
import android.view.ViewGroup;
import android.widget.BaseAdapter;
import android.widget.CheckBox;
import android.widget.ImageButton;
import android.widget.LinearLayout;
import android.widget.ListView;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONObject;

import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * Trusted-device permission management (protocol v3, host-enforced).
 *
 * Lists trusted devices from PERMS_LIST, toggles the 7 permission flags per
 * device via PERMS_SET. The host rejects disallowed message types at runtime
 * with PERMS_DENIED (surfaced as "Blocked by host permissions"). Your own
 * device row is disabled — the host rejects self-permission changes.
 *
 * Requires a v3 host; older hosts simply never answer PERMS_LIST and the
 * screen says so honestly.
 */
public class UserManagementActivity extends Activity implements RemoteClient.Listener {

    public static final String EXTRA_HOST = "host";
    public static final String EXTRA_PORT = "port";
    public static final String EXTRA_PASSWORD = "password";

    private static final long LIST_TIMEOUT_MS = 8000;

    private static class Device {
        String deviceId;
        String deviceName;
        String platform;
        long firstSeen;
        long lastSeen;
        final Map<String, Boolean> permissions = new LinkedHashMap<String, Boolean>();
    }

    private RemoteClient client;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private final List<Device> devices = new ArrayList<Device>();
    private BaseAdapter adapter;
    private TextView statusText;
    private String ownDeviceId;
    private boolean listReceived;
    private final Runnable listTimeout = new Runnable() {
        @Override public void run() {
            if (!listReceived) {
                statusText.setText(R.string.perms_unsupported);
                statusText.setVisibility(View.VISIBLE);
            }
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_users);

        final String host = getIntent().getStringExtra(EXTRA_HOST);
        final int port = getIntent().getIntExtra(EXTRA_PORT, RemoteProto.PORT);
        final String password = getIntent().getStringExtra(EXTRA_PASSWORD);
        if (host == null || password == null) {
            finish();
            return;
        }
        ownDeviceId = ownDeviceId(this);

        statusText = findViewById(R.id.perms_status);
        ListView list = findViewById(R.id.users_list);
        adapter = new BaseAdapter() {
            @Override public int getCount() { return devices.size(); }
            @Override public Object getItem(int p) { return devices.get(p); }
            @Override public long getItemId(int p) { return p; }
            @Override
            public View getView(int position, View cv, ViewGroup parent) {
                return deviceRow(devices.get(position), parent);
            }
        };
        list.setAdapter(adapter);

        ImageButton back = findViewById(R.id.btn_users_back);
        back.setOnClickListener(v -> finish());

        client = new RemoteClient(this, host, port, password, this);
        client.start();
    }

    @Override
    protected void onDestroy() {
        ui.removeCallbacks(listTimeout);
        if (client != null) {
            client.sendDisconnect();
            client.stop();
            client = null;
        }
        super.onDestroy();
    }

    private static String ownDeviceId(Context ctx) {
        try {
            String id = Settings.Secure.getString(ctx.getContentResolver(),
                    Settings.Secure.ANDROID_ID);
            return id == null ? "unknown" : id;
        } catch (Exception e) {
            return "unknown";
        }
    }

    // ---- row ------------------------------------------------------------------

    private View deviceRow(final Device d, ViewGroup parent) {
        View v = LayoutInflater.from(this).inflate(R.layout.item_device_perms,
                parent, false);
        TextView name = v.findViewById(R.id.dev_name);
        TextView meta = v.findViewById(R.id.dev_meta);
        LinearLayout permsBox = v.findViewById(R.id.dev_perms);
        TextView selfNote = v.findViewById(R.id.dev_self_note);

        name.setText(d.deviceName == null || d.deviceName.isEmpty()
                ? d.deviceId : d.deviceName);
        SimpleDateFormat f = new SimpleDateFormat("MMM d HH:mm", Locale.US);
        String seen = d.lastSeen > 0 ? f.format(new Date(d.lastSeen)) : "—";
        meta.setText((d.platform == null ? "" : d.platform + " · ")
                + "last seen " + seen);

        final boolean isSelf = d.deviceId != null && d.deviceId.equals(ownDeviceId);
        selfNote.setVisibility(isSelf ? View.VISIBLE : View.GONE);
        if (isSelf) selfNote.setText(R.string.perms_self_note);

        permsBox.removeAllViews();
        for (final String perm : RemoteProto.PERMISSIONS) {
            CheckBox cb = new CheckBox(this);
            cb.setText(perm);
            cb.setTextColor(getResources().getColor(R.color.text, null));
            cb.setTextSize(13);
            Boolean on = d.permissions.get(perm);
            cb.setChecked(Boolean.TRUE.equals(on));
            cb.setEnabled(!isSelf);
            if (!isSelf) {
                cb.setOnCheckedChangeListener((buttonView, isChecked) -> {
                    d.permissions.put(perm, isChecked);
                    try {
                        JSONObject p = new JSONObject();
                        for (Map.Entry<String, Boolean> e : d.permissions.entrySet()) {
                            p.put(e.getKey(), e.getValue());
                        }
                        client.sendPermsSet(d.deviceId, p);
                    } catch (Exception ignored) {
                    }
                });
            }
            permsBox.addView(cb);
        }
        return v;
    }

    // ---- RemoteClient.Listener --------------------------------------------------

    @Override
    public void onConnected() {
        ui.post(() -> {
            listReceived = false;
            statusText.setText(R.string.reconnecting);
            statusText.setVisibility(View.VISIBLE);
            client.sendPermsList();
            ui.postDelayed(listTimeout, LIST_TIMEOUT_MS);
        });
    }

    @Override public void onAuthFailed(String reason) {
        ui.post(() -> {
            Toast.makeText(this, R.string.auth_failed, Toast.LENGTH_LONG).show();
            finish();
        });
    }

    @Override public void onDisconnected(boolean willRetry, String reason) { }

    @Override public void onFrame(byte[] jpeg) { }
    @Override public void onStats(int fps, long rttMs) { }
    @Override public void onClipboardText(String text) { }
    @Override public void onFileMsg(int type, byte[] payload) { }

    @Override
    public void onPermsList(final String json) {
        ui.post(() -> {
            listReceived = true;
            ui.removeCallbacks(listTimeout);
            statusText.setVisibility(View.GONE);
            devices.clear();
            try {
                JSONArray arr = new JSONObject(json).optJSONArray("devices");
                if (arr != null) {
                    for (int i = 0; i < arr.length(); i++) {
                        JSONObject o = arr.getJSONObject(i);
                        Device d = new Device();
                        d.deviceId = o.optString("device_id", "");
                        d.deviceName = o.optString("device_name", "");
                        d.platform = o.optString("platform", "");
                        d.firstSeen = o.optLong("first_seen", 0);
                        d.lastSeen = o.optLong("last_seen", 0);
                        JSONObject p = o.optJSONObject("permissions");
                        for (String perm : RemoteProto.PERMISSIONS) {
                            d.permissions.put(perm,
                                    p != null && p.optBoolean(perm, false));
                        }
                        devices.add(d);
                    }
                }
            } catch (Exception ignored) {
            }
            // own device first
            for (int i = 1; i < devices.size(); i++) {
                if (ownDeviceId.equals(devices.get(i).deviceId)) {
                    Device self = devices.remove(i);
                    devices.add(0, self);
                    break;
                }
            }
            adapter.notifyDataSetChanged();
            if (devices.isEmpty()) {
                statusText.setText(R.string.perms_empty);
                statusText.setVisibility(View.VISIBLE);
            }
        });
    }

    @Override
    public void onPermsResp(final String deviceId, final boolean ok, final String detail) {
        ui.post(() -> {
            if (ok) {
                Toast.makeText(this, R.string.perms_saved, Toast.LENGTH_SHORT).show();
            } else {
                Toast.makeText(this,
                        getString(R.string.perms_save_failed, detail),
                        Toast.LENGTH_LONG).show();
                client.sendPermsList(); // re-sync to the host's truth
            }
        });
    }

    @Override
    public void onPermsDenied(final String op, final String reason) {
        ui.post(() -> Toast.makeText(this,
                getString(R.string.perms_denied, reason),
                Toast.LENGTH_LONG).show());
    }
}
