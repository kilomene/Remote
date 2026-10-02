package com.remote.viewer;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.os.Build;
import android.os.Bundle;
import android.text.InputType;
import android.view.LayoutInflater;
import android.view.View;
import android.view.ViewGroup;
import android.widget.BaseAdapter;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.ImageButton;
import android.widget.LinearLayout;
import android.widget.ListView;
import android.widget.TextView;
import android.widget.Toast;

import com.remote.viewer.features.security.BiometricLock;
import com.remote.viewer.features.session.SessionDb;

import org.json.JSONArray;
import org.json.JSONObject;

import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;
import java.util.Locale;

/**
 * Security center: biometric app lock, trusted (paired) devices,
 * session-timeout setting, security-event log.
 *
 * Trusted devices: the client keeps its own paired-hosts list (hosts it has
 * successfully authenticated to). "Revoke" deletes the entry locally;
 * host-side revoke is planned (the host holds the authoritative store).
 */
public class SecurityCenterActivity extends Activity {

    private static final String KEY_TRUSTED = "trusted_hosts";

    private CheckBox biometricBox;
    private ListView trustedList;
    private final List<String> trusted = new ArrayList<String>();
    private BaseAdapter trustedAdapter;
    private TextView eventsText;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_security);

        ImageButton back = findViewById(R.id.btn_sec_back);
        back.setOnClickListener(v -> finish());

        // ---- biometric lock ----
        LinearLayout bioRow = findViewById(R.id.sec_bio_row);
        biometricBox = findViewById(R.id.sec_biometric);
        if (!BiometricLock.isAvailable(this)) {
            bioRow.setVisibility(View.GONE);
        } else {
            biometricBox.setChecked(Prefs.getBool(this, Prefs.K_BIOMETRIC, false));
            biometricBox.setOnCheckedChangeListener((b, checked) -> {
                if (checked) {
                    // verify the user can authenticate before enabling
                    BiometricLock.authenticate(this, new BiometricLock.Callback() {
                        @Override public void onAuthenticated() {
                            Prefs.putBool(SecurityCenterActivity.this,
                                    Prefs.K_BIOMETRIC, true);
                            Toast.makeText(SecurityCenterActivity.this,
                                    R.string.sec_bio_on, Toast.LENGTH_SHORT).show();
                        }
                        @Override public void onFailed() {
                            biometricBox.setChecked(false);
                        }
                    });
                } else {
                    Prefs.putBool(this, Prefs.K_BIOMETRIC, false);
                }
            });
        }

        // ---- session timeout ----
        TextView timeoutVal = findViewById(R.id.sec_timeout_val);
        updateTimeoutLabel(timeoutVal);
        View timeoutRow = findViewById(R.id.sec_timeout_row);
        timeoutRow.setOnClickListener(v -> showTimeoutDialog(timeoutVal));

        // ---- trusted devices ----
        trustedList = findViewById(R.id.sec_trusted_list);
        trustedAdapter = new BaseAdapter() {
            @Override public int getCount() { return trusted.size(); }
            @Override public Object getItem(int p) { return trusted.get(p); }
            @Override public long getItemId(int p) { return p; }
            @Override
            public View getView(int position, View cv, ViewGroup parent) {
                final String entry = trusted.get(position);
                View v = cv;
                if (v == null) {
                    v = LayoutInflater.from(SecurityCenterActivity.this)
                            .inflate(R.layout.item_trusted, parent, false);
                }
                TextView name = v.findViewById(R.id.trusted_name);
                name.setText(entry);
                v.findViewById(R.id.btn_trusted_revoke).setOnClickListener(vv ->
                        new AlertDialog.Builder(SecurityCenterActivity.this)
                                .setMessage(getString(R.string.sec_revoke_confirm, entry))
                                .setPositiveButton(R.string.sec_revoke, (d, w) -> {
                                    revokeTrusted(entry);
                                    SessionDb.get(SecurityCenterActivity.this)
                                            .logEvent("trust-revoke", entry);
                                })
                                .setNegativeButton(R.string.cancel, null)
                                .show());
                return v;
            }
        };
        trustedList.setAdapter(trustedAdapter);
        loadTrusted();

        TextView planned = findViewById(R.id.sec_trusted_note);
        planned.setText(R.string.sec_trusted_note);

        // ---- security events ----
        eventsText = findViewById(R.id.sec_events);
        renderEvents();
    }

    @Override
    protected void onResume() {
        super.onResume();
        loadTrusted();
        renderEvents();
    }

    // ---- session timeout ----

    private void updateTimeoutLabel(TextView tv) {
        int mins = Prefs.getInt(this, Prefs.K_SESSION_TIMEOUT_MIN, 0);
        tv.setText(mins <= 0 ? getString(R.string.sec_timeout_off)
                : getString(R.string.sec_timeout_mins, mins));
    }

    private void showTimeoutDialog(final TextView label) {
        final String[] opts = {"Off", "15 min", "30 min", "1 hour", "4 hours"};
        final int[] vals = {0, 15, 30, 60, 240};
        int cur = Prefs.getInt(this, Prefs.K_SESSION_TIMEOUT_MIN, 0);
        int sel = 0;
        for (int i = 0; i < vals.length; i++) if (vals[i] == cur) sel = i;
        new AlertDialog.Builder(this)
                .setTitle(R.string.sec_timeout)
                .setSingleChoiceItems(opts, sel, (d, which) -> {
                    Prefs.putInt(this, Prefs.K_SESSION_TIMEOUT_MIN, vals[which]);
                    updateTimeoutLabel(label);
                    d.dismiss();
                })
                .show();
    }

    // ---- trusted devices ----

    private static List<String> loadTrustedList(android.content.Context ctx) {
        List<String> out = new ArrayList<String>();
        try {
            JSONArray arr = new JSONArray(Prefs.getString(ctx, KEY_TRUSTED, "[]"));
            for (int i = 0; i < arr.length(); i++) out.add(arr.getString(i));
        } catch (Exception ignored) {
        }
        return out;
    }

    /** Records a host as trusted after a successful auth (idempotent). */
    public static void markTrusted(android.content.Context ctx, String hostPort) {
        if (hostPort == null) return;
        List<String> cur = loadTrustedList(ctx);
        if (!cur.contains(hostPort)) {
            cur.add(hostPort);
            saveTrustedList(ctx, cur);
        }
    }

    private static void saveTrustedList(android.content.Context ctx, List<String> list) {
        Prefs.putString(ctx, KEY_TRUSTED, new JSONArray(list).toString());
    }

    private void loadTrusted() {
        trusted.clear();
        trusted.addAll(loadTrustedList(this));
        trustedAdapter.notifyDataSetChanged();
        findViewById(R.id.sec_trusted_empty)
                .setVisibility(trusted.isEmpty() ? View.VISIBLE : View.GONE);
    }

    private void revokeTrusted(String entry) {
        List<String> cur = loadTrustedList(this);
        cur.remove(entry);
        saveTrustedList(this, cur);
        loadTrusted();
        Toast.makeText(this, R.string.sec_revoked, Toast.LENGTH_SHORT).show();
    }

    // ---- events ----

    private void renderEvents() {
        List<SessionDb.EventRow> events = SessionDb.get(this).listEvents(30);
        if (events.isEmpty()) {
            eventsText.setText(R.string.history_empty);
            return;
        }
        SimpleDateFormat f = new SimpleDateFormat("MMM d HH:mm", Locale.US);
        StringBuilder sb = new StringBuilder();
        for (SessionDb.EventRow e : events) {
            sb.append(f.format(new Date(e.ts))).append("  ·  ")
                    .append(e.kind);
            if (!e.detail.isEmpty()) sb.append(" — ").append(e.detail);
            sb.append('\n');
        }
        eventsText.setText(sb.toString().trim());
    }

    /** Biometric gate used by MainActivity when the lock is enabled. */
    public static void gate(final Activity activity, final Runnable onUnlocked) {
        if (!BiometricLock.isLockEnabled(activity)) {
            onUnlocked.run();
            return;
        }
        BiometricLock.authenticate(activity, new BiometricLock.Callback() {
            @Override public void onAuthenticated() { onUnlocked.run(); }
            @Override public void onFailed() { activity.finish(); }
        });
    }
}
