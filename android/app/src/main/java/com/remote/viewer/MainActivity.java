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
import android.widget.EditText;
import android.widget.ImageButton;
import android.widget.LinearLayout;
import android.widget.TextView;

import java.net.InetSocketAddress;
import java.net.Socket;
import java.util.Collections;
import java.util.Comparator;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * Home screen: "MY COMPUTERS" device list.
 * Rows show name, host:port and an online/offline dot (2s TCP probe).
 * Tap connects, long-press offers pin/unpin + delete, + adds a new host.
 */
public class MainActivity extends Activity {

    private LinearLayout savedList;
    private TextView emptySaved;
    private SecureStore store;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private ExecutorService probes = Executors.newCachedThreadPool();

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        store = new SecureStore(this);
        savedList = findViewById(R.id.saved_list);
        emptySaved = findViewById(R.id.empty_saved);

        ImageButton btnAdd = findViewById(R.id.btn_add);
        btnAdd.setOnClickListener(v -> showAddDialog());

        ImageButton btnSettings = findViewById(R.id.btn_settings);
        btnSettings.setOnClickListener(v ->
                startActivity(new Intent(this, SettingsActivity.class)));
    }

    @Override
    protected void onResume() {
        super.onResume();
        refreshHosts();
    }

    @Override
    protected void onDestroy() {
        probes.shutdownNow();
        super.onDestroy();
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
            final View dot = row.findViewById(R.id.host_dot);
            name.setText((h.favorite ? "\u2605 " : "") + h.name);
            addr.setText(h.host + ":" + h.port);
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
    }

    private void probeOnline(final SecureStore.Host h, final View dot) {
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
                        dot.getBackground().mutate().setTint(getResources().getColor(
                                ok ? R.color.success : R.color.text_faint, null));
                        dot.setContentDescription(ok ? "online" : "offline");
                    }
                });
            }
        });
    }

    private void connect(SecureStore.Host h) {
        Intent i = new Intent(this, SessionActivity.class);
        i.putExtra(SessionActivity.EXTRA_HOST, h.host);
        i.putExtra(SessionActivity.EXTRA_PORT, h.port);
        i.putExtra(SessionActivity.EXTRA_PASSWORD, h.password);
        startActivity(i);
    }

    private void showHostOptions(final SecureStore.Host h) {
        final String pinLabel = h.favorite ? getString(R.string.unpin) : getString(R.string.pin);
        new AlertDialog.Builder(this)
                .setTitle(h.name)
                .setItems(new CharSequence[]{pinLabel, getString(R.string.delete)},
                        (d, which) -> {
                            if (which == 0) {
                                store.upsert(new SecureStore.Host(h.name, h.host, h.port,
                                        h.password, !h.favorite));
                                refreshHosts();
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

    private void showAddDialog() {
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

        new AlertDialog.Builder(this)
                .setTitle(R.string.dlg_add_host)
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
                    store.upsert(new SecureStore.Host(name, host, port, password));
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
