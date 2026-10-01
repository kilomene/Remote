package com.remote.viewer;

import android.app.Activity;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Bundle;
import android.widget.Button;
import android.widget.EditText;

/** Connection screen: Tailscale IP/hostname + port + password. */
public class MainActivity extends Activity {

    private static final String PREFS = "remote_viewer";
    private EditText hostField, portField, passwordField;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        hostField = findViewById(R.id.field_host);
        portField = findViewById(R.id.field_port);
        passwordField = findViewById(R.id.field_password);
        Button connect = findViewById(R.id.btn_connect);

        SharedPreferences prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
        hostField.setText(prefs.getString("host", ""));
        portField.setText(String.valueOf(prefs.getInt("port", RemoteProto.PORT)));

        connect.setOnClickListener(v -> {
            String host = hostField.getText().toString().trim();
            String password = passwordField.getText().toString();
            int port;
            try {
                port = Integer.parseInt(portField.getText().toString().trim());
            } catch (NumberFormatException e) {
                port = RemoteProto.PORT;
            }
            if (host.isEmpty() || password.isEmpty()) {
                passwordField.setError("host and password are required");
                return;
            }
            prefs.edit().putString("host", host).putInt("port", port).apply();
            Intent i = new Intent(this, StreamActivity.class);
            i.putExtra(StreamActivity.EXTRA_HOST, host);
            i.putExtra(StreamActivity.EXTRA_PORT, port);
            i.putExtra(StreamActivity.EXTRA_PASSWORD, password);
            startActivity(i);
        });
    }
}
