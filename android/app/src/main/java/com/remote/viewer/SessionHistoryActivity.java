package com.remote.viewer;

import android.app.Activity;
import android.app.AlertDialog;
import android.os.Bundle;
import android.view.LayoutInflater;
import android.view.View;
import android.view.ViewGroup;
import android.widget.BaseAdapter;
import android.widget.ImageButton;
import android.widget.ListView;
import android.widget.TextView;

import com.remote.viewer.features.session.SessionDb;

import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;
import java.util.Locale;

/**
 * Session history: past sessions (device, start/end, duration, bytes),
 * security events (auth failures, …) and transfer history. Viewer-side
 * SQLite (SessionDb).
 */
public class SessionHistoryActivity extends Activity {

    private static final int LIMIT = 200;

    private ListView list;
    private final List<String> rows = new ArrayList<String>();
    private BaseAdapter adapter;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_history);

        list = findViewById(R.id.history_list);
        adapter = new BaseAdapter() {
            @Override public int getCount() { return rows.size(); }
            @Override public Object getItem(int p) { return rows.get(p); }
            @Override public long getItemId(int p) { return p; }
            @Override
            public View getView(int position, View cv, ViewGroup parent) {
                TextView tv = (TextView) cv;
                if (tv == null) {
                    tv = (TextView) LayoutInflater.from(SessionHistoryActivity.this)
                            .inflate(android.R.layout.simple_list_item_1, parent, false);
                    tv.setTextColor(getResources().getColor(R.color.text, null));
                    tv.setTextSize(13);
                    tv.setPadding(8, 10, 8, 10);
                }
                tv.setText(rows.get(position));
                return tv;
            }
        };
        list.setAdapter(adapter);

        ImageButton back = findViewById(R.id.btn_history_back);
        back.setOnClickListener(v -> finish());

        ImageButton clear = findViewById(R.id.btn_history_clear);
        clear.setOnClickListener(v -> new AlertDialog.Builder(this)
                .setMessage(R.string.history_clear_confirm)
                .setPositiveButton(R.string.delete, (d, w) -> {
                    SessionDb.get(this).clearSessions();
                    load();
                })
                .setNegativeButton(R.string.cancel, null)
                .show());

        load();
    }

    @Override
    protected void onResume() {
        super.onResume();
        load();
    }

    private void load() {
        rows.clear();
        SimpleDateFormat f = new SimpleDateFormat("MMM d HH:mm", Locale.US);
        SessionDb db = SessionDb.get(this);

        rows.add("— " + getString(R.string.history_sessions) + " —");
        List<SessionDb.SessionRow> sessions = db.listSessions(LIMIT);
        if (sessions.isEmpty()) rows.add(getString(R.string.history_empty));
        for (SessionDb.SessionRow s : sessions) {
            String when = f.format(new Date(s.started));
            String dur = s.ended > 0 ? fmtDur((s.ended - s.started) / 1000) : "…";
            rows.add(s.device + "  ·  " + when + "  ·  " + dur
                    + "  ·  ↓" + human(s.rxBytes) + " ↑" + human(s.txBytes));
        }

        rows.add("— " + getString(R.string.history_security) + " —");
        List<SessionDb.EventRow> events = db.listEvents(LIMIT);
        if (events.isEmpty()) rows.add(getString(R.string.history_empty));
        for (SessionDb.EventRow e : events) {
            rows.add(f.format(new Date(e.ts)) + "  ·  " + e.kind
                    + (e.detail.isEmpty() ? "" : " — " + e.detail));
        }

        rows.add("— " + getString(R.string.history_transfers) + " —");
        List<SessionDb.TransferRow> transfers = db.listTransfers(LIMIT);
        if (transfers.isEmpty()) rows.add(getString(R.string.history_empty));
        for (SessionDb.TransferRow t : transfers) {
            rows.add(f.format(new Date(t.ts)) + "  ·  "
                    + ("up".equals(t.direction) ? "↑" : "↓") + " " + t.path
                    + "  ·  " + human(t.size) + "  ·  " + t.status);
        }
        adapter.notifyDataSetChanged();
    }

    private static String fmtDur(long s) {
        if (s < 60) return s + "s";
        if (s < 3600) return (s / 60) + "m " + (s % 60) + "s";
        return (s / 3600) + "h " + ((s % 3600) / 60) + "m";
    }

    private static String human(long bytes) {
        if (bytes < 1024) return bytes + "B";
        double kb = bytes / 1024.0;
        if (kb < 1024) return String.format(Locale.US, "%.0fK", kb);
        double mb = kb / 1024.0;
        if (mb < 1024) return String.format(Locale.US, "%.1fM", mb);
        return String.format(Locale.US, "%.2fG", mb / 1024.0);
    }
}
