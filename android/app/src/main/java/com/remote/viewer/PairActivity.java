package com.remote.viewer;

import android.app.Activity;
import android.content.Intent;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.ImageButton;
import android.widget.TextView;

/**
 * Manual host entry (v4 connect flow).
 *
 * The host is reached with plain password auth: the user enters the host,
 * port and password here and is taken straight to the session
 * (SessionActivity). Pairing codes are not used — the host does not
 * require pairing.
 */
public class PairActivity extends Activity {

    private final Handler ui = new Handler(Looper.getMainLooper());
    private EditText hostF, portF, passF, nameF;
    private TextView status;
    private Button pairBtn;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_pair);

        hostF = findViewById(R.id.pair_host);
        portF = findViewById(R.id.pair_port);
        passF = findViewById(R.id.pair_password);
        nameF = findViewById(R.id.pair_device_name);
        status = findViewById(R.id.pair_status);
        pairBtn = findViewById(R.id.btn_pair_go);

        nameF.setText(Build.MODEL);

        ImageButton back = findViewById(R.id.btn_pair_back);
        back.setOnClickListener(v -> finish());

        pairBtn.setOnClickListener(v -> startPairing());
    }

    private void setStatus(final String text, final int colorRes) {
        ui.post(() -> {
            if (text == null) {
                status.setVisibility(View.GONE);
            } else {
                status.setText(text);
                status.setTextColor(getResources().getColor(colorRes, null));
                status.setVisibility(View.VISIBLE);
            }
        });
    }

    private void startPairing() {
        final String host = hostF.getText().toString().trim();
        final String password = passF.getText().toString();
        int port;
        try {
            port = Integer.parseInt(portF.getText().toString().trim());
        } catch (NumberFormatException e) {
            port = RemoteProto.PORT;
        }
        final int portFv = port;

        if (host.isEmpty() || password.isEmpty()) {
            setStatus(getString(R.string.pair_need_host), R.color.danger);
            return;
        }

        // No pairing code: the host does not require pairing, so connect
        // directly with password auth.
        String label = host;
        new SecureStore(this).upsert(
                new SecureStore.Host(label, host, portFv, password));
        Intent i = new Intent(this, SessionActivity.class);
        i.putExtra(SessionActivity.EXTRA_HOST, host);
        i.putExtra(SessionActivity.EXTRA_PORT, portFv);
        i.putExtra(SessionActivity.EXTRA_PASSWORD, password);
        i.putExtra("device_label", label);
        startActivity(i);
        finish();
    }
}
