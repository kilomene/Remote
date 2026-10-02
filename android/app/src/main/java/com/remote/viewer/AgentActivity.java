package com.remote.viewer;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.LayoutInflater;
import android.view.View;
import android.view.ViewGroup;
import android.widget.BaseAdapter;
import android.widget.Button;
import android.widget.ImageButton;
import android.widget.ListView;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/**
 * Agent dashboard (protocol v3): live CPU/RAM/disk/net gauges plus the
 * host's service list, with real start/stop/restart via SYSTEM_CMD
 * "service" {name, action} (the host allowlists the service name).
 * "View logs" opens a remote terminal running `journalctl -u <name>`.
 */
public class AgentActivity extends Activity implements RemoteClient.Listener {

    public static final String EXTRA_HOST = "host";
    public static final String EXTRA_PORT = "port";
    public static final String EXTRA_PASSWORD = "password";

    private static class Service {
        String name;
        boolean active;
    }

    private RemoteClient client;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private final Runnable poller = new Runnable() {
        @Override public void run() {
            if (client != null) client.sendAgentQuery();
            ui.postDelayed(this, 5000);
        }
    };

    private ProgressBar cpuBar, memBar, diskBar;
    private TextView cpuText, memText, diskText, netText, updatedText;
    private ListView servicesList;
    private final List<Service> services = new ArrayList<Service>();
    private BaseAdapter adapter;

    private String host;
    private int port;
    private String password;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_agent);

        host = getIntent().getStringExtra(EXTRA_HOST);
        port = getIntent().getIntExtra(EXTRA_PORT, RemoteProto.PORT);
        password = getIntent().getStringExtra(EXTRA_PASSWORD);
        if (host == null || password == null) {
            finish();
            return;
        }

        cpuBar = findViewById(R.id.agent_cpu_bar);
        memBar = findViewById(R.id.agent_mem_bar);
        diskBar = findViewById(R.id.agent_disk_bar);
        cpuText = findViewById(R.id.agent_cpu_text);
        memText = findViewById(R.id.agent_mem_text);
        diskText = findViewById(R.id.agent_disk_text);
        netText = findViewById(R.id.agent_net_text);
        updatedText = findViewById(R.id.agent_updated);
        servicesList = findViewById(R.id.agent_services);

        adapter = new BaseAdapter() {
            @Override public int getCount() { return services.size(); }
            @Override public Object getItem(int p) { return services.get(p); }
            @Override public long getItemId(int p) { return p; }
            @Override
            public View getView(int position, View cv, ViewGroup parent) {
                View v = cv;
                if (v == null) {
                    v = LayoutInflater.from(AgentActivity.this)
                            .inflate(R.layout.item_service, parent, false);
                }
                final Service s = services.get(position);
                TextView name = v.findViewById(R.id.svc_name);
                View dot = v.findViewById(R.id.svc_dot);
                Button logs = v.findViewById(R.id.svc_logs);
                Button start = v.findViewById(R.id.svc_start);
                Button stop = v.findViewById(R.id.svc_stop);
                Button restart = v.findViewById(R.id.svc_restart);
                name.setText(s.name);
                dot.getBackground().mutate().setTint(getResources().getColor(
                        s.active ? R.color.success : R.color.text_faint, null));
                logs.setOnClickListener(vv -> openLogs(s.name));
                start.setOnClickListener(vv -> confirmServiceAction(s, "start"));
                stop.setOnClickListener(vv -> confirmServiceAction(s, "stop"));
                restart.setOnClickListener(vv -> confirmServiceAction(s, "restart"));
                start.setVisibility(s.active ? View.GONE : View.VISIBLE);
                stop.setVisibility(s.active ? View.VISIBLE : View.GONE);
                return v;
            }
        };
        servicesList.setAdapter(adapter);

        TextView planned = findViewById(R.id.agent_planned_note);
        planned.setText(R.string.agent_service_note);

        ImageButton back = findViewById(R.id.btn_agent_back);
        back.setOnClickListener(v -> finish());

        client = new RemoteClient(this, host, port, password, this);
        client.start();
    }

    private void openLogs(String service) {
        Intent i = new Intent(this, TerminalActivity.class);
        i.putExtra(TerminalActivity.EXTRA_HOST, host);
        i.putExtra(TerminalActivity.EXTRA_PORT, port);
        i.putExtra(TerminalActivity.EXTRA_PASSWORD, password);
        i.putExtra(TerminalActivity.EXTRA_COMMAND, "journalctl -u " + service + " -n 100 -f");
        startActivity(i);
    }

    /** Confirms then sends SYSTEM_CMD "service" {name, action}. */
    private void confirmServiceAction(final Service s, final String action) {
        new AlertDialog.Builder(this)
                .setMessage(getString(R.string.agent_service_confirm, action, s.name))
                .setPositiveButton(R.string.ok, (d, w) -> {
                    try {
                        JSONObject args = new JSONObject();
                        args.put("name", s.name);
                        args.put("action", action);
                        client.sendSystemCmd("service", args);
                    } catch (Exception ignored) {
                    }
                })
                .setNegativeButton(R.string.cancel, null)
                .show();
    }

    @Override
    protected void onDestroy() {
        ui.removeCallbacks(poller);
        if (client != null) {
            client.sendDisconnect();
            client.stop();
            client = null;
        }
        super.onDestroy();
    }

    // ---- RemoteClient.Listener ----------------------------------------------

    @Override public void onConnected() {
        ui.post(() -> {
            client.sendAgentQuery();
            ui.postDelayed(poller, 5000);
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
    public void onAgentStatus(final String json) {
        ui.post(() -> render(json));
    }

    @Override
    public void onSystemResp(final String cmd, final boolean ok, final String detail) {
        ui.post(() -> {
            Toast.makeText(this,
                    com.remote.viewer.features.system.SystemActions.formatResp(
                            cmd, ok, detail),
                    Toast.LENGTH_LONG).show();
            if (client != null) client.sendAgentQuery(); // refresh service states
        });
    }

    @Override
    public void onPermsDenied(final String op, final String reason) {
        ui.post(() -> Toast.makeText(this,
                getString(R.string.perms_denied, reason),
                Toast.LENGTH_LONG).show());
    }

    private void render(String json) {
        try {
            JSONObject o = new JSONObject(json);
            double cpu = o.optDouble("cpu_pct", -1);
            long memTotal = o.optLong("mem_total_mb", 0);
            long memUsed = o.optLong("mem_used_mb", 0);
            long diskTotal = o.optLong("disk_total_gb", 0);
            long diskUsed = o.optLong("disk_used_gb", 0);
            long rx = o.optLong("net_rx_bps", 0);
            long tx = o.optLong("net_tx_bps", 0);

            if (cpu >= 0) {
                cpuBar.setProgress((int) Math.min(100, cpu));
                cpuText.setText(String.format(Locale.US, "%.1f%%", cpu));
            } else {
                cpuText.setText("—");
            }
            if (memTotal > 0) {
                int pct = (int) (memUsed * 100 / memTotal);
                memBar.setProgress(pct);
                memText.setText(memUsed + " / " + memTotal + " MB");
            } else {
                memText.setText("—");
            }
            if (diskTotal > 0) {
                int pct = (int) (diskUsed * 100 / diskTotal);
                diskBar.setProgress(pct);
                diskText.setText(diskUsed + " / " + diskTotal + " GB");
            } else {
                diskText.setText("—");
            }
            netText.setText("↓ " + humanBps(rx) + "   ↑ " + humanBps(tx));
            updatedText.setText(getString(R.string.agent_updated_at,
                    new java.text.SimpleDateFormat("HH:mm:ss", Locale.US)
                            .format(new java.util.Date(o.optLong("ts",
                                    System.currentTimeMillis())))));

            JSONArray arr = o.optJSONArray("services");
            if (arr != null) {
                services.clear();
                for (int i = 0; i < arr.length(); i++) {
                    JSONObject s = arr.getJSONObject(i);
                    Service svc = new Service();
                    svc.name = s.optString("name", "?");
                    svc.active = s.optBoolean("active", false);
                    services.add(svc);
                }
                adapter.notifyDataSetChanged();
            }
        } catch (Exception ignored) {
        }
    }

    private static String humanBps(long bps) {
        if (bps < 1000) return bps + " b/s";
        double kb = bps / 1000.0;
        if (kb < 1000) return String.format(Locale.US, "%.1f Kb/s", kb);
        return String.format(Locale.US, "%.2f Mb/s", kb / 1000.0);
    }
}
