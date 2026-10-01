package com.remote.viewer;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
import android.view.LayoutInflater;
import android.view.View;
import android.widget.ArrayAdapter;
import android.widget.EditText;
import android.widget.ImageButton;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.Spinner;
import android.widget.TextView;

import com.remote.viewer.features.notify.Notify;

import org.json.JSONObject;

import java.net.InetSocketAddress;
import java.net.Socket;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.Date;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * Home screen: "MY COMPUTERS" device list.
 * Rows show name, host:port, icon, tags, online/offline dot (2s TCP probe),
 * and harvested metadata (resolution · last seen). Tap connects, long-press
 * offers pin/unpin + edit + delete, + adds a new host.
 *
 * Hosts that go offline after the initial probe round raise a notification
 * (while the app is open). Biometric lock (if enabled) gates app entry.
 */
public class MainActivity extends Activity {

    private LinearLayout savedList;
    private TextView emptySaved;
    private SecureStore store;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private ExecutorService probes = Executors.newCachedThreadPool();
    private final Map<String, Boolean> lastOnline = new HashMap<String, Boolean>();
    private boolean firstRound = true;
    private boolean paused;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        store = new SecureStore(this);
        savedList = findViewById(R.id.saved_list);
        emptySaved = findViewById(R.id.empty_saved);

        ImageButton btnAdd = findViewById(R.id.btn_add);
        btnAdd.setOnClickListener(v -> showHostDialog(null));

        ImageButton btnSettings = findViewById(R.id.btn_settings);
        btnSettings.setOnClickListener(v ->
                startActivity(new Intent(this, SettingsActivity.class)));

        ImageButton btnSecurity = findViewById(R.id.btn_security);
        btnSecurity.setOnClickListener(v ->
                startActivity(new Intent(this, SecurityCenterActivity.class)));

        ImageButton btnHistory = findViewById(R.id.btn_history);
        btnHistory.setOnClickListener(v ->
                startActivity(new Intent(this, SessionHistoryActivity.class)));
    }

    @Override
    protected void onResume() {
        super.onResume();
        paused = false;
        // biometric lock actually gates app entry
        SecurityCenterActivity.gate(this, new Runnable() {
            @Override public void run() {
                refreshHosts();
            }
        });
    }

    @Override
    protected void onPause() {
        paused = true;
        super.onPause();
    }

    @Override
    protected void onDestroy() {
        probes.shutdownNow();
        super.onDestroy();
    }

    private static int hostIconRes(int icon) {
        return icon == 1 ? R.drawable.ic_laptop
                : icon == 2 ? R.drawable.ic_server : R.drawable.ic_pc;
    }

    private String hostMetaLine(String host, int port) {
        try {
            JSONObject all = new JSONObject(
                    Prefs.getString(this, Prefs.K_HOST_META, "{}"));
            JSONObject m = all.optJSONObject(host + ":" + port);
            if (m == null) return null;
            String res = m.optString("resolution", "");
            long seen = m.optLong("last_seen", 0);
            String when = seen > 0
                    ? new SimpleDateFormat("MMM d HH:mm", Locale.US)
                            .format(new Date(seen))
                    : "—";
            return (res.isEmpty() ? "" : res + " · ") + "last seen " + when;
        } catch (Exception e) {
            return null;
        }
    }

    private void refreshHosts() {
        List<SecureStore.Host> hosts = store.load();
        Collections.sort(hosts, new Comparator<SecureStore.Host>() {
            @Override public int compare(SecureStore.Host a, SecureStore.Host b) {
                if (a.favorite != b.favorite) return a.favorite ? -1 : 1;
                return a.name.compareToIgnoreCase(b.name);
            }
        });
        savedList.removeAllViews();
        emptySaved.setVisibility(hosts.isEmpty() ? View.VISIBLE : View.GONE);
        LayoutInflater inf = LayoutInflater.from(this);
        for (final SecureStore.Host h : hosts) {
            View row = inf.inflate(R.layout.item_host, savedList, false);
            TextView name = row.findViewById(R.id.host_name);
            TextView addr = row.findViewById(R.id.host_addr);
            TextView meta = row.findViewById(R.id.host_meta);
            ImageView icon = row.findViewById(R.id.host_icon);
            final View dot = row.findViewById(R.id.host_dot);
            String label = (h.favorite ? "\u2605 " : "") + h.name;
            if (!h.tags.isEmpty()) label += "  [" + h.tags + "]";
            name.setText(label);
            addr.setText(h.host + ":" + h.port);
            String metaLine = hostMetaLine(h.host, h.port);
            meta.setVisibility(metaLine == null ? View.GONE : View.VISIBLE);
            if (metaLine != null) meta.setText(metaLine);
            icon.setImageResource(hostIconRes(h.icon));
            dot.getBackground().mutate().setTint(
                    getResources().getColor(R.color.text_faint, null));
            row.findViewById(R.id.btn_host_delete).setOnClickListener(v ->
                    confirmDelete(h));
            row.setOnClickListener(v -> connect(h));
            row.setOnLongClickListener(v -> {
                showHostOptions(h);
                return true;
            });
            savedList.addView(row);
            probeOnline(h, dot);
        }
        firstRound = true;
    }

    private void probeOnline(final SecureStore.Host h, final View dot) {
        final String key = h.host + ":" + h.port;
        probes.execute(new Runnable() {
            @Override public void run() {
                boolean online = false;
                try {
                    Socket s = new Socket();
                    s.connect(new InetSocketAddress(h.host, h.port), 2000);
                    s.close();
                    online = true;
                } catch (Exception ignored) {
                }
                final boolean ok = online;
                ui.post(new Runnable() {
                    @Override public void run() {
                        if (paused) return;
                        dot.getBackground().mutate().setTint(getResources().getColor(
                                ok ? R.color.success : R.color.text_faint, null));
                        dot.setContentDescription(ok ? "online" : "offline");
                        Boolean prev = lastOnline.put(key, ok);
                        // notify only on online->offline transitions, and only
                        // after the initial probe round has established state
                        if (!firstRound && prev != null && prev && !ok) {
                            Notify.hostOffline(MainActivity.this, h.name);
                        }
                        if (prev != null && !prev && ok) {
                            Notify.dismissHostOffline(MainActivity.this, h.name);
                        }
                    }
                });
            }
        });
        // end the initial round after this batch of probes was posted
        ui.post(new Runnable() {
            @Override public void run() {
                firstRound = false;
            }
        });
    }

    private void connect(SecureStore.Host h) {
        Intent i = new Intent(this, SessionActivity.class);
        i.putExtra(SessionActivity.EXTRA_HOST, h.host);
        i.putExtra(SessionActivity.EXTRA_PORT, h.port);
        i.putExtra(SessionActivity.EXTRA_PASSWORD, h.password);
        i.putExtra("device_label", h.name);
        startActivity(i);
    }

    private void showHostOptions(final SecureStore.Host h) {
        final String pinLabel = h.favorite ? getString(R.string.unpin) : getString(R.string.pin);
        new AlertDialog.Builder(this)
                .setTitle(h.name)
                .setItems(new CharSequence[]{pinLabel, getString(R.string.edit),
                                getString(R.string.delete)},
                        (d, which) -> {
                            if (which == 0) {
                                store.upsert(new SecureStore.Host(h.name, h.host,
                                        h.port, h.password, !h.favorite, h.tags, h.icon));
                                refreshHosts();
                            } else if (which == 1) {
                                showHostDialog(h);
                            } else {
                                confirmDelete(h);
                            }
                        })
                .show();
    }

    private void confirmDelete(final SecureStore.Host h) {
        new AlertDialog.Builder(this)
                .setMessage(R.string.delete_host)
                .setPositiveButton(R.string.delete, (d, w) -> {
                    store.delete(h);
                    refreshHosts();
                })
                .setNegativeButton(R.string.cancel, null)
                .show();
    }

    private void showHostDialog(final SecureStore.Host existing) {
        LinearLayout form = new LinearLayout(this);
        form.setOrientation(LinearLayout.VERTICAL);
        int pad = (int) (16 * getResources().getDisplayMetrics().density);
        form.setPadding(pad, pad / 2, pad, pad / 2);

        final EditText nameF = dialogField(form, getString(R.string.dlg_name),
                InputType.TYPE_CLASS_TEXT);
        final EditText hostF = dialogField(form, getString(R.string.hint_host),
                InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        final EditText portF = dialogField(form, "47800", InputType.TYPE_CLASS_NUMBER);
        final EditText passF = dialogField(form, getString(R.string.hint_password),
                InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        final EditText tagsF = dialogField(form, getString(R.string.dlg_tags),
                InputType.TYPE_CLASS_TEXT);
        final Spinner iconF = new Spinner(this);
        ArrayAdapter<String> iconAdapter = new ArrayAdapter<String>(this,
                android.R.layout.simple_spinner_item,
                new String[]{getString(R.string.icon_pc),
                        getString(R.string.icon_laptop),
                        getString(R.string.icon_server)});
        iconAdapter.setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item);
        iconF.setAdapter(iconAdapter);
        form.addView(iconF);

        if (existing != null) {
            nameF.setText(existing.name);
            hostF.setText(existing.host);
            portF.setText(String.valueOf(existing.port));
            passF.setText(existing.password);
            tagsF.setText(existing.tags);
            iconF.setSelection(Math.max(0, Math.min(2, existing.icon)));
        }

        new AlertDialog.Builder(this)
                .setTitle(existing == null ? R.string.dlg_add_host : R.string.edit)
                .setView(form)
                .setPositiveButton(R.string.save, (d, w) -> {
                    String host = hostF.getText().toString().trim();
                    String password = passF.getText().toString();
                    if (host.isEmpty() || password.isEmpty()) {
                        hostF.setError("host and password are required");
                        return;
                    }
                    int port;
                    try {
                        port = Integer.parseInt(portF.getText().toString().trim());
                    } catch (NumberFormatException e) {
                        port = RemoteProto.PORT;
                    }
                    String name = nameF.getText().toString().trim();
                    if (name.isEmpty()) name = host;
                    String tags = tagsF.getText().toString().trim();
                    int icon = iconF.getSelectedItemPosition();
                    boolean fav = existing != null && existing.favorite;
                    store.upsert(new SecureStore.Host(name, host, port, password,
                            fav, tags, icon));
                    refreshHosts();
                })
                .setNegativeButton(R.string.cancel, null)
                .show();
    }

    private EditText dialogField(LinearLayout form, String hint, int inputType) {
        EditText f = new EditText(this);
        f.setHint(hint);
        f.setInputType(inputType);
        f.setSingleLine(true);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT);
        lp.topMargin = (int) (8 * getResources().getDisplayMetrics().density);
        f.setLayoutParams(lp);
        form.addView(f);
        return f;
    }
}
