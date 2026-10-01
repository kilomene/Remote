package com.remote.viewer.features.terminal;

import android.content.Context;

import com.remote.viewer.RemoteClient;

import java.util.HashMap;
import java.util.Map;

/**
 * v3 remote terminal client (0x61-0x63).
 *
 * Owns a RemoteClient connection and routes terminal messages to per-session
 * listeners. Multiple sessions (tabs) are supported: TERMINAL_OPEN replies
 * TERMINAL_OPENED with a server-assigned session id, matched back to the
 * pending open request.
 */
public class TerminalClient implements RemoteClient.Listener {

    public interface SessionListener {
        void onOpened(String session);
        void onData(String session, byte[] data);
        void onClosed(String session);
    }

    public interface StatusListener {
        void onConnected();
        void onAuthFailed(String reason);
        void onDisconnected(boolean willRetry, String reason);
    }

    private final RemoteClient client;
    private StatusListener statusListener;
    private final Map<String, SessionListener> sessions =
            new HashMap<String, SessionListener>();
    private SessionListener pendingListener;

    public TerminalClient(Context ctx, String host, int port, String password) {
        this.client = new RemoteClient(ctx, host, port, password, this);
    }

    public void setStatusListener(StatusListener l) {
        this.statusListener = l;
    }

    public void start() {
        client.start();
    }

    public void stop() {
        client.stop();
    }

    public void sendDisconnect() {
        client.sendDisconnect();
    }

    /** Opens a terminal; the listener receives onOpened with the session id. */
    public void open(int cols, int rows, SessionListener listener) {
        pendingListener = listener;
        client.sendTerminalOpen(cols, rows);
    }

    public void write(String session, byte[] data) {
        client.sendTerminalData(session, data);
    }

    public void write(String session, String text) {
        try {
            write(session, text.getBytes("UTF-8"));
        } catch (Exception ignored) {
        }
    }

    public void close(String session) {
        client.sendTerminalClose(session);
        SessionListener l;
        synchronized (sessions) {
            l = sessions.remove(session);
        }
        if (l != null) l.onClosed(session);
    }

    public boolean hasOpenSessions() {
        synchronized (sessions) {
            return !sessions.isEmpty();
        }
    }

    // ---- RemoteClient.Listener ----------------------------------------------

    @Override public void onConnected() {
        StatusListener l = statusListener;
        if (l != null) l.onConnected();
    }

    @Override public void onAuthFailed(String reason) {
        StatusListener l = statusListener;
        if (l != null) l.onAuthFailed(reason);
    }

    @Override public void onDisconnected(boolean willRetry, String reason) {
        StatusListener l = statusListener;
        if (l != null) l.onDisconnected(willRetry, reason);
    }

    @Override public void onFrame(byte[] jpeg) { }
    @Override public void onStats(int fps, long rttMs) { }
    @Override public void onClipboardText(String text) { }
    @Override public void onFileMsg(int type, byte[] payload) { }

    @Override
    public void onTerminalOpened(String session) {
        SessionListener l = pendingListener;
        pendingListener = null;
        if (l != null) {
            synchronized (sessions) {
                sessions.put(session, l);
            }
            l.onOpened(session);
        }
    }

    @Override
    public void onTerminalData(String session, byte[] data) {
        SessionListener l;
        synchronized (sessions) {
            l = sessions.get(session);
        }
        if (l != null) l.onData(session, data);
    }

    @Override
    public void onTerminalClosed(String session) {
        SessionListener l;
        synchronized (sessions) {
            l = sessions.remove(session);
        }
        if (l != null) l.onClosed(session);
    }
}
