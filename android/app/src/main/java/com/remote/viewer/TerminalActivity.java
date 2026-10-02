package com.remote.viewer;

import android.app.Activity;
import android.content.Context;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.View;
import android.view.inputmethod.InputMethodManager;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.ImageButton;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

import com.remote.viewer.features.terminal.TerminalClient;
import com.remote.viewer.features.terminal.TerminalView;

import java.util.ArrayList;
import java.util.List;

/**
 * Remote terminal: full-screen dark terminal backed by v3 TERMINAL_*
 * messages. Multiple sessions as tabs; each tab owns a TerminalClient
 * session id and a TerminalView. Optional EXTRA_COMMAND runs a shell
 * command immediately after the pty opens (used by "view logs").
 */
public class TerminalActivity extends Activity {

    public static final String EXTRA_HOST = "host";
    public static final String EXTRA_PORT = "port";
    public static final String EXTRA_PASSWORD = "password";
    public static final String EXTRA_COMMAND = "command";

    private static class Tab {
        String sessionId;
        TerminalView view;
        Button tabBtn;
        boolean opened;
    }

    private TerminalClient term;
    private FrameLayout termHolder;
    private LinearLayout tabBar;
    private TextView statusText;
    private final List<Tab> tabs = new ArrayList<Tab>();
    private Tab active;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private String pendingCommand;
    private int tabCount;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_terminal);

        final String host = getIntent().getStringExtra(EXTRA_HOST);
        final int port = getIntent().getIntExtra(EXTRA_PORT, RemoteProto.PORT);
        final String password = getIntent().getStringExtra(EXTRA_PASSWORD);
        pendingCommand = getIntent().getStringExtra(EXTRA_COMMAND);
        if (host == null || password == null) {
            finish();
            return;
        }

        termHolder = findViewById(R.id.term_holder);
        tabBar = findViewById(R.id.term_tabs);
        statusText = findViewById(R.id.term_status);

        ImageButton back = findViewById(R.id.btn_term_back);
        back.setOnClickListener(v -> finish());

        ImageButton newTab = findViewById(R.id.btn_term_new);
        newTab.setOnClickListener(v -> openTab());

        ImageButton keyboard = findViewById(R.id.btn_term_keyboard);
        keyboard.setOnClickListener(v -> {
            if (active != null) {
                active.view.requestFocus();
                InputMethodManager imm = (InputMethodManager)
                        getSystemService(Context.INPUT_METHOD_SERVICE);
                if (imm != null) imm.showSoftInput(active.view, 0);
            }
        });

        buildKeyRow();

        term = new TerminalClient(this, host, port, password);
        term.setStatusListener(new TerminalClient.StatusListener() {
            @Override public void onConnected() {
                ui.post(() -> {
                    statusText.setVisibility(View.GONE);
                    if (tabs.isEmpty()) openTab();
                });
            }
            @Override public void onAuthFailed(String reason) {
                ui.post(() -> {
                    Toast.makeText(TerminalActivity.this,
                            R.string.auth_failed, Toast.LENGTH_LONG).show();
                    finish();
                });
            }
            @Override public void onDisconnected(boolean willRetry, String reason) {
                ui.post(() -> {
                    if (willRetry) {
                        statusText.setText(R.string.reconnecting);
                        statusText.setVisibility(View.VISIBLE);
                    } else {
                        Toast.makeText(TerminalActivity.this,
                                R.string.disconnected, Toast.LENGTH_SHORT).show();
                        finish();
                    }
                });
            }
        });
        statusText.setText(R.string.reconnecting);
        term.start();
    }

    private void buildKeyRow() {
        LinearLayout row = findViewById(R.id.term_keyrow);
        String[] keys = {"esc", "tab", "ctrl-c", "ctrl-d", "ctrl-z", "ctrl-l",
                "up", "down", "left", "right", "home", "end", "pgup", "pgdn", "del"};
        for (final String k : keys) {
            Button b = new Button(this, null, 0, R.style.RemoteKeyButton);
            b.setText(k.toUpperCase().replace("CTRL-", "^"));
            b.setOnClickListener(v -> {
                if (active != null) active.view.sendSpecial(k);
            });
            LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                    LinearLayout.LayoutParams.WRAP_CONTENT,
                    LinearLayout.LayoutParams.WRAP_CONTENT);
            int m = (int) (4 * getResources().getDisplayMetrics().density);
            lp.setMargins(m, m, m, m);
            b.setLayoutParams(lp);
            row.addView(b);
        }
    }

    private void openTab() {
        final Tab tab = new Tab();
        tab.view = new TerminalView(this);
        tab.view.setFontSizeSp(Prefs.getInt(this, Prefs.K_TERM_FONT, 14));
        tab.view.setVisibility(View.GONE);
        tab.view.setInputSink(data -> {
            if (tab.sessionId != null && term != null) {
                term.write(tab.sessionId, data);
            }
        });
        tab.view.setOnTouchListener((v, e) -> false);
        termHolder.addView(tab.view,
                new FrameLayout.LayoutParams(
                        FrameLayout.LayoutParams.MATCH_PARENT,
                        FrameLayout.LayoutParams.MATCH_PARENT));
        tab.tabBtn = new Button(this, null, 0, R.style.RemoteTabButton);
        tabCount++;
        tab.tabBtn.setText("sh" + tabCount);
        tab.tabBtn.setOnClickListener(v -> activate(tab));
        tab.tabBtn.setOnLongClickListener(v -> {
            closeTab(tab);
            return true;
        });
        tabBar.addView(tab.tabBtn);
        tabs.add(tab);
        activate(tab);
        // 80x24 is a sane default; TerminalView reports its real grid.
        term.open(80, 24, new TerminalClient.SessionListener() {
            @Override public void onOpened(String session) {
                ui.post(() -> {
                    tab.sessionId = session;
                    tab.opened = true;
                    String cmd = pendingCommand;
                    pendingCommand = null; // only the first tab runs it
                    if (cmd != null && !cmd.isEmpty()) {
                        term.write(session, cmd + "\n");
                    }
                });
            }
            @Override public void onData(String session, byte[] data) {
                ui.post(() -> tab.view.append(data));
            }
            @Override public void onClosed(String session) {
                ui.post(() -> closeTab(tab));
            }
        });
        tab.view.requestFocus();
    }

    private void activate(Tab tab) {
        active = tab;
        for (Tab t : tabs) {
            t.view.setVisibility(t == tab ? View.VISIBLE : View.GONE);
            t.tabBtn.setSelected(t == tab);
        }
    }

    private void closeTab(Tab tab) {
        if (tab.sessionId != null && term != null) {
            term.close(tab.sessionId);
        }
        tabs.remove(tab);
        tabBar.removeView(tab.tabBtn);
        termHolder.removeView(tab.view);
        if (tabs.isEmpty()) {
            finish();
        } else if (active == tab) {
            activate(tabs.get(tabs.size() - 1));
        }
    }

    @Override
    protected void onDestroy() {
        if (term != null) {
            term.sendDisconnect();
            term.stop();
            term = null;
        }
        super.onDestroy();
    }
}
