package com.remote.viewer.features.system;

import android.app.Activity;
import android.app.AlertDialog;
import android.text.InputType;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.Toast;

import com.remote.viewer.R;
import com.remote.viewer.RemoteClient;

import org.json.JSONException;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;

/**
 * Linux quick actions over SYSTEM_CMD (0x60, protocol v3).
 *
 * Whitelisted host commands — the host only executes an explicit allow list
 * (same jail-by-default philosophy as file transfer), so unknown cmds are
 * rejected server-side with SYSTEM_RESP {ok:false}.
 */
public final class SystemActions {

    private SystemActions() {}

    public static final String LOCK = "lock";
    public static final String LOGOUT = "logout";
    public static final String REBOOT = "reboot";
    public static final String SHUTDOWN = "shutdown";
    public static final String SUSPEND = "suspend";
    public static final String OPEN_TERMINAL = "open-terminal";
    public static final String OPEN_BROWSER = "open-browser";
    public static final String OPEN_APP = "open-app";
    public static final String BLANK_SCREEN = "blank-screen";
    public static final String SWITCH_DISPLAY = "switch-display";

    /** Actions that need an explicit "are you sure" before sending. */
    private static boolean isDestructive(String cmd) {
        return REBOOT.equals(cmd) || SHUTDOWN.equals(cmd)
                || LOGOUT.equals(cmd) || SUSPEND.equals(cmd);
    }

    private static String label(Activity a, String cmd) {
        int id;
        if (LOCK.equals(cmd)) id = R.string.qa_lock;
        else if (OPEN_TERMINAL.equals(cmd)) id = R.string.qa_terminal;
        else if (OPEN_BROWSER.equals(cmd)) id = R.string.qa_browser;
        else if (SUSPEND.equals(cmd)) id = R.string.qa_suspend;
        else if (LOGOUT.equals(cmd)) id = R.string.qa_logout;
        else if (REBOOT.equals(cmd)) id = R.string.qa_reboot;
        else if (SHUTDOWN.equals(cmd)) id = R.string.qa_shutdown;
        else return cmd;
        return a.getString(id);
    }

    /** Shows the quick-action picker. client may be null-safe (ignored). */
    public static void show(final Activity activity, final RemoteClient client) {
        if (client == null) {
            Toast.makeText(activity, R.string.not_connected, Toast.LENGTH_SHORT).show();
            return;
        }
        final String[] cmds = {LOCK, OPEN_TERMINAL, OPEN_BROWSER,
                SUSPEND, LOGOUT, REBOOT, SHUTDOWN};
        List<CharSequence> labels = new ArrayList<CharSequence>();
        for (String c : cmds) labels.add(label(activity, c));
        new AlertDialog.Builder(activity)
                .setTitle(R.string.qa_title)
                .setItems(labels.toArray(new CharSequence[0]), (d, which) -> {
                    String cmd = cmds[which];
                    if (OPEN_BROWSER.equals(cmd)) {
                        promptUrl(activity, client);
                    } else if (isDestructive(cmd)) {
                        new AlertDialog.Builder(activity)
                                .setMessage(activity.getString(
                                        R.string.qa_confirm, label(activity, cmd)))
                                .setPositiveButton(R.string.ok,
                                        (dd, w) -> send(activity, client, cmd, null))
                                .setNegativeButton(R.string.cancel, null)
                                .show();
                    } else {
                        send(activity, client, cmd, null);
                    }
                })
                .show();
    }

    private static void promptUrl(final Activity activity, final RemoteClient client) {
        final EditText f = new EditText(activity);
        f.setHint("https://");
        f.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        f.setSingleLine(true);
        int pad = (int) (20 * activity.getResources().getDisplayMetrics().density);
        f.setPadding(pad, pad / 2, pad, pad / 2);
        new AlertDialog.Builder(activity)
                .setTitle(R.string.qa_browser)
                .setView(f)
                .setPositiveButton(R.string.ok, (d, w) -> {
                    String url = f.getText().toString().trim();
                    if (url.isEmpty()) return;
                    try {
                        JSONObject args = new JSONObject();
                        args.put("url", url);
                        send(activity, client, OPEN_BROWSER, args);
                    } catch (JSONException ignored) {
                    }
                })
                .setNegativeButton(R.string.cancel, null)
                .show();
    }

    private static void send(Activity activity, RemoteClient client,
                             String cmd, JSONObject args) {
        client.sendSystemCmd(cmd, args);
        Toast.makeText(activity, label(activity, cmd), Toast.LENGTH_SHORT).show();
    }

    /** Toggles the privacy (blank-screen) mode on the host. */
    public static void setPrivacy(RemoteClient client, boolean enabled) {
        if (client == null) return;
        try {
            JSONObject args = new JSONObject();
            args.put("enabled", enabled);
            client.sendSystemCmd(BLANK_SCREEN, args);
        } catch (JSONException ignored) {
        }
    }

    /** Switches the host's active display (from DISPLAYS_LIST ids). */
    public static void switchDisplay(RemoteClient client, String displayId) {
        if (client == null || displayId == null) return;
        try {
            JSONObject args = new JSONObject();
            args.put("id", displayId);
            client.sendSystemCmd(SWITCH_DISPLAY, args);
        } catch (JSONException ignored) {
        }
    }

    /** Formats a SYSTEM_RESP for a toast/log line. */
    public static String formatResp(String cmd, boolean ok, String detail) {
        StringBuilder sb = new StringBuilder();
        sb.append(cmd).append(ok ? ": ok" : ": failed");
        if (detail != null && !detail.isEmpty()) sb.append(" — ").append(detail);
        return sb.toString();
    }
}
