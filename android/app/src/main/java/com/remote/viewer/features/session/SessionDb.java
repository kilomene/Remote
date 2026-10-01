package com.remote.viewer.features.session;

import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.database.sqlite.SQLiteDatabase;
import android.database.sqlite.SQLiteOpenHelper;

import java.util.ArrayList;
import java.util.List;

/**
 * Local SQLite store for:
 * - session history (device, start/end, duration, bytes transferred)
 * - security events (auth failures, revocations, …)
 * - transfer history (direction, path, size, status)
 *
 * Viewer-side only; the host keeps its own logs.
 */
public class SessionDb extends SQLiteOpenHelper {

    private static final String DB = "remote_sessions.db";
    private static final int VER = 1;

    public static class SessionRow {
        public long id;
        public String device;
        public String host;
        public long started;
        public long ended;
        public long rxBytes;
        public long txBytes;
    }

    public static class EventRow {
        public long id;
        public long ts;
        public String kind;
        public String detail;
    }

    public static class TransferRow {
        public long id;
        public long ts;
        public String direction; // up|down
        public String path;
        public long size;
        public String status;    // done|failed|cancelled
    }

    private static SessionDb instance;

    public static synchronized SessionDb get(Context ctx) {
        if (instance == null) {
            instance = new SessionDb(ctx.getApplicationContext());
        }
        return instance;
    }

    private SessionDb(Context ctx) {
        super(ctx, DB, null, VER);
    }

    @Override
    public void onCreate(SQLiteDatabase db) {
        db.execSQL("CREATE TABLE sessions (_id INTEGER PRIMARY KEY AUTOINCREMENT,"
                + "device TEXT, host TEXT, started INTEGER, ended INTEGER,"
                + "rx_bytes INTEGER, tx_bytes INTEGER)");
        db.execSQL("CREATE TABLE events (_id INTEGER PRIMARY KEY AUTOINCREMENT,"
                + "ts INTEGER, kind TEXT, detail TEXT)");
        db.execSQL("CREATE TABLE transfers (_id INTEGER PRIMARY KEY AUTOINCREMENT,"
                + "ts INTEGER, direction TEXT, path TEXT, size INTEGER, status TEXT)");
        db.execSQL("CREATE INDEX idx_sessions_started ON sessions(started)");
        db.execSQL("CREATE INDEX idx_events_ts ON events(ts)");
        db.execSQL("CREATE INDEX idx_transfers_ts ON transfers(ts)");
    }

    @Override
    public void onUpgrade(SQLiteDatabase db, int oldVersion, int newVersion) {
        // v1: nothing to migrate yet.
    }

    // ---- sessions -----------------------------------------------------------

    /** Inserts a session row, returns the row id (pass to endSession). */
    public synchronized long startSession(String device, String host, long started) {
        ContentValues v = new ContentValues();
        v.put("device", device);
        v.put("host", host);
        v.put("started", started);
        v.put("ended", 0L);
        v.put("rx_bytes", 0L);
        v.put("tx_bytes", 0L);
        return getWritableDatabase().insert("sessions", null, v);
    }

    public synchronized void endSession(long id, long ended, long rxBytes, long txBytes) {
        ContentValues v = new ContentValues();
        v.put("ended", ended);
        v.put("rx_bytes", rxBytes);
        v.put("tx_bytes", txBytes);
        getWritableDatabase().update("sessions", v, "_id=?",
                new String[]{String.valueOf(id)});
    }

    public synchronized List<SessionRow> listSessions(int limit) {
        List<SessionRow> out = new ArrayList<SessionRow>();
        Cursor c = null;
        try {
            c = getReadableDatabase().query("sessions", null, null, null,
                    null, null, "started DESC", String.valueOf(limit));
            while (c.moveToNext()) {
                SessionRow r = new SessionRow();
                r.id = c.getLong(0);
                r.device = c.getString(1);
                r.host = c.getString(2);
                r.started = c.getLong(3);
                r.ended = c.getLong(4);
                r.rxBytes = c.getLong(5);
                r.txBytes = c.getLong(6);
                out.add(r);
            }
        } finally {
            if (c != null) c.close();
        }
        return out;
    }

    public synchronized void clearSessions() {
        getWritableDatabase().delete("sessions", null, null);
    }

    // ---- security events ----------------------------------------------------

    public synchronized void logEvent(String kind, String detail) {
        ContentValues v = new ContentValues();
        v.put("ts", System.currentTimeMillis());
        v.put("kind", kind);
        v.put("detail", detail == null ? "" : detail);
        getWritableDatabase().insert("events", null, v);
    }

    public synchronized List<EventRow> listEvents(int limit) {
        List<EventRow> out = new ArrayList<EventRow>();
        Cursor c = null;
        try {
            c = getReadableDatabase().query("events", null, null, null,
                    null, null, "ts DESC", String.valueOf(limit));
            while (c.moveToNext()) {
                EventRow r = new EventRow();
                r.id = c.getLong(0);
                r.ts = c.getLong(1);
                r.kind = c.getString(2);
                r.detail = c.getString(3);
                out.add(r);
            }
        } finally {
            if (c != null) c.close();
        }
        return out;
    }

    // ---- transfer history ---------------------------------------------------

    public synchronized void logTransfer(String direction, String path,
                                         long size, String status) {
        ContentValues v = new ContentValues();
        v.put("ts", System.currentTimeMillis());
        v.put("direction", direction);
        v.put("path", path == null ? "" : path);
        v.put("size", size);
        v.put("status", status);
        getWritableDatabase().insert("transfers", null, v);
    }

    public synchronized List<TransferRow> listTransfers(int limit) {
        List<TransferRow> out = new ArrayList<TransferRow>();
        Cursor c = null;
        try {
            c = getReadableDatabase().query("transfers", null, null, null,
                    null, null, "ts DESC", String.valueOf(limit));
            while (c.moveToNext()) {
                TransferRow r = new TransferRow();
                r.id = c.getLong(0);
                r.ts = c.getLong(1);
                r.direction = c.getString(2);
                r.path = c.getString(3);
                r.size = c.getLong(4);
                r.status = c.getString(5);
                out.add(r);
            }
        } finally {
            if (c != null) c.close();
        }
        return out;
    }
}
