package com.remote.viewer;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.content.pm.PackageInfo;
import android.os.Build;
import android.os.Bundle;
import android.view.View;
import android.widget.ArrayAdapter;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.CompoundButton;
import android.widget.EditText;
import android.widget.SeekBar;
import android.widget.Spinner;
import android.widget.TextView;
import android.widget.Toast;

import com.remote.viewer.features.security.BiometricLock;

import org.json.JSONArray;

/**
 * Settings, in sections: Connection, Controls, Display, Clipboard,
 * Security, Notifications, Shortcuts, plus saved-host wipe and about.
 */
public class SettingsActivity extends Activity {

    private static final int[] FPS_VALUES = {15, 30, 45, 60};
    private static final String[] QUALITY_VALUES = {"ultra", "high", "balanced", "low", "minlat"};
    private static final int[] TIMEOUT_VALUES = {0, 15, 30, 60, 240};

    private boolean binding;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_settings);
        bind();
        bindActions();
        refreshShortcutsPreview();
        bindAbout();
    }

    @Override
    protected void onResume() {
        super.onResume();
        bind(); // pick up changes made in SecurityCenter
    }

    // ---- bind ---------------------------------------------------------------

    private void bind() {
        binding = true;
        try {
            Prefs prefs = Prefs.load(this);

            Spinner quality = findViewById(R.id.set_quality);
            setStringAdapter(quality, new String[]{
                    getString(R.string.q_ultra), getString(R.string.q_high),
                    getString(R.string.q_balanced), getString(R.string.q_low),
                    getString(R.string.q_minlat)});
            quality.setSelection(indexOf(QUALITY_VALUES,
                    Prefs.getString(this, Prefs.K_QUALITY, "balanced"), 2));

            Spinner fps = findViewById(R.id.set_fps);
            setStringAdapter(fps, new String[]{"15 fps", "30 fps", "45 fps", "60 fps"});
            fps.setSelection(indexOf(FPS_VALUES, prefs.fpsCap, 1));

            check(R.id.set_reconnect, Prefs.getBool(this, Prefs.K_AUTORECONNECT, true));

            Spinner mode = findViewById(R.id.set_control_mode);
            setStringAdapter(mode, new String[]{getString(R.string.mode_touch),
                    getString(R.string.mode_trackpad), getString(R.string.mode_mouse)});
            String cm = Prefs.getString(this, Prefs.K_CONTROL_MODE, "touch");
            mode.setSelection("trackpad".equals(cm) ? 1 : "mouse".equals(cm) ? 2 : 0);

            SeekBar sens = findViewById(R.id.set_sensitivity);
            int s = Prefs.getInt(this, Prefs.K_SENSITIVITY, 100);
            sens.setProgress(s - 50);
            updateSensitivityLabel(s);

            check(R.id.set_precision, Prefs.getBool(this, Prefs.K_PRECISION, false));
            check(R.id.set_haptics, prefs.haptics);

            Spinner scaling = findViewById(R.id.set_scaling);
            setStringAdapter(scaling, new String[]{getString(R.string.scale_fit),
                    getString(R.string.scale_original)});
            scaling.setSelection("original".equals(
                    Prefs.getString(this, Prefs.K_SCALE_MODE, "fit")) ? 1 : 0);

            check(R.id.set_keepawake, prefs.keepAwake);
            check(R.id.set_smooth, prefs.smoothScaling);
            check(R.id.set_clipboard, prefs.clipboard);

            CheckBox bio = findViewById(R.id.set_biometric);
            boolean bioVisible = Build.VERSION.SDK_INT >= 29
                    && BiometricLock.isAvailable(this);
            bio.setVisibility(bioVisible ? View.VISIBLE : View.GONE);
            bio.setChecked(BiometricLock.isLockEnabled(this));

            Spinner timeout = findViewById(R.id.set_timeout);
            setStringAdapter(timeout, new String[]{getString(R.string.timeout_off),
                    "15 " + getString(R.string.minutes),
                    "30 " + getString(R.string.minutes),
                    "60 " + getString(R.string.minutes),
                    "240 " + getString(R.string.minutes)});
            timeout.setSelection(indexOf(TIMEOUT_VALUES,
                    Prefs.getInt(this, Prefs.K_SESSION_TIMEOUT_MIN, 0), 0));

            check(R.id.set_notif_session, Prefs.getBool(this, Prefs.K_NOTIF_SESSION, true));
            check(R.id.set_notif_transfer, Prefs.getBool(this, Prefs.K_NOTIF_TRANSFER, true));
            check(R.id.set_notif_offline, Prefs.getBool(this, Prefs.K_NOTIF_OFFLINE, true));
        } finally {
            binding = false;
        }
    }

    private void setStringAdapter(Spinner s, String[] items) {
        ArrayAdapter<String> a = new ArrayAdapter<String>(this,
                android.R.layout.simple_spinner_item, items);
        a.setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item);
        s.setAdapter(a);
    }

    private static int indexOf(int[] arr, int val, int def) {
        for (int i = 0; i < arr.length; i++) if (arr[i] == val) return i;
        return def;
    }

    private static int indexOf(String[] arr, String val, int def) {
        for (int i = 0; i < arr.length; i++) if (arr[i].equals(val)) return i;
        return def;
    }

    private void check(int id, boolean on) {
        ((CheckBox) findViewById(id)).setChecked(on);
    }

    private void updateSensitivityLabel(int s) {
        ((TextView) findViewById(R.id.sensitivity_val)).setText(s + "%");
    }

    // ---- actions --------------------------------------------------------------

    private void bindActions() {
        CompoundButton.OnCheckedChangeListener saver =
                (buttonView, isChecked) -> { if (!binding) saveAll(); };
        int[] boxes = {R.id.set_reconnect, R.id.set_precision, R.id.set_haptics,
                R.id.set_keepawake, R.id.set_smooth, R.id.set_clipboard,
                R.id.set_notif_session, R.id.set_notif_transfer, R.id.set_notif_offline};
        for (int id : boxes) {
            ((CheckBox) findViewById(id)).setOnCheckedChangeListener(saver);
        }

        android.widget.AdapterView.OnItemSelectedListener spinSaver =
                new android.widget.AdapterView.OnItemSelectedListener() {
                    @Override public void onItemSelected(
                            android.widget.AdapterView<?> p, View v, int pos, long id) {
                        if (!binding) saveAll();
                    }
                    @Override public void onNothingSelected(
                            android.widget.AdapterView<?> p) { }
                };
        int[] spinners = {R.id.set_quality, R.id.set_fps, R.id.set_control_mode,
                R.id.set_scaling, R.id.set_timeout};
        for (int id : spinners) {
            ((Spinner) findViewById(id)).setOnItemSelectedListener(spinSaver);
        }

        SeekBar sens = findViewById(R.id.set_sensitivity);
        sens.setOnSeekBarChangeListener(new SeekBar.OnSeekBarChangeListener() {
            @Override public void onProgressChanged(SeekBar s, int p, boolean fromUser) {
                int v = p + 50;
                updateSensitivityLabel(v);
                if (fromUser && !binding) saveAll();
            }
            @Override public void onStartTrackingTouch(SeekBar s) { }
            @Override public void onStopTrackingTouch(SeekBar s) { }
        });

        // biometric: verify with a real prompt before enabling
        CheckBox bio = findViewById(R.id.set_biometric);
        bio.setOnCheckedChangeListener((buttonView, isChecked) -> {
            if (binding) return;
            if (isChecked && !BiometricLock.isLockEnabled(this)) {
                BiometricLock.authenticate(this, new BiometricLock.Callback() {
                    @Override public void onAuthenticated() {
                        Prefs.putBool(SettingsActivity.this, Prefs.K_BIOMETRIC, true);
                        Toast.makeText(SettingsActivity.this,
                                R.string.bio_enabled, Toast.LENGTH_SHORT).show();
                    }
                    @Override public void onFailed() {
                        Prefs.putBool(SettingsActivity.this, Prefs.K_BIOMETRIC, false);
                        ((CheckBox) findViewById(R.id.set_biometric))
                                .setChecked(false);
                        Toast.makeText(SettingsActivity.this,
                                R.string.auth_failed, Toast.LENGTH_LONG).show();
                    }
                });
            } else if (!isChecked) {
                Prefs.putBool(this, Prefs.K_BIOMETRIC, false);
            }
        });

        Button secCenter = findViewById(R.id.btn_security_center);
        secCenter.setOnClickListener(v ->
                startActivity(new Intent(this, SecurityCenterActivity.class)));

        Button editShortcuts = findViewById(R.id.btn_edit_shortcuts);
        editShortcuts.setOnClickListener(v -> editShortcuts());

        Button clear = findViewById(R.id.btn_clear);
        clear.setOnClickListener(v -> new AlertDialog.Builder(this)
                .setMessage(R.string.set_clear_sub)
                .setPositiveButton(R.string.set_clear, (d, w) -> {
                    new SecureStore(SettingsActivity.this).clear();
                    Toast.makeText(this, R.string.hosts_cleared, Toast.LENGTH_SHORT).show();
                })
                .setNegativeButton(R.string.cancel, null)
                .show());
    }

    private void saveAll() {
        Prefs cur = Prefs.load(this);
        int qualityPos = ((Spinner) findViewById(R.id.set_quality)).getSelectedItemPosition();
        int fpsPos = ((Spinner) findViewById(R.id.set_fps)).getSelectedItemPosition();
        int modePos = ((Spinner) findViewById(R.id.set_control_mode)).getSelectedItemPosition();
        int scalePos = ((Spinner) findViewById(R.id.set_scaling)).getSelectedItemPosition();
        int timeoutPos = ((Spinner) findViewById(R.id.set_timeout)).getSelectedItemPosition();

        Prefs.save(this,
                ((CheckBox) findViewById(R.id.set_haptics)).isChecked(),
                ((CheckBox) findViewById(R.id.set_keepawake)).isChecked(),
                ((CheckBox) findViewById(R.id.set_smooth)).isChecked(),
                ((CheckBox) findViewById(R.id.set_clipboard)).isChecked(),
                FPS_VALUES[clamp(fpsPos, 0, FPS_VALUES.length - 1)]);
        Prefs.putString(this, Prefs.K_QUALITY, QUALITY_VALUES[clamp(qualityPos, 0, 4)]);
        Prefs.putBool(this, Prefs.K_AUTORECONNECT,
                ((CheckBox) findViewById(R.id.set_reconnect)).isChecked());
        Prefs.putString(this, Prefs.K_CONTROL_MODE,
                modePos == 1 ? "trackpad" : modePos == 2 ? "mouse" : "touch");
        Prefs.putBool(this, Prefs.K_PRECISION,
                ((CheckBox) findViewById(R.id.set_precision)).isChecked());
        Prefs.putInt(this, Prefs.K_SENSITIVITY,
                ((SeekBar) findViewById(R.id.set_sensitivity)).getProgress() + 50);
        Prefs.putString(this, Prefs.K_SCALE_MODE,
                scalePos == 1 ? "original" : "fit");
        Prefs.putInt(this, Prefs.K_SESSION_TIMEOUT_MIN,
                TIMEOUT_VALUES[clamp(timeoutPos, 0, TIMEOUT_VALUES.length - 1)]);
        Prefs.putBool(this, Prefs.K_NOTIF_SESSION,
                ((CheckBox) findViewById(R.id.set_notif_session)).isChecked());
        Prefs.putBool(this, Prefs.K_NOTIF_TRANSFER,
                ((CheckBox) findViewById(R.id.set_notif_transfer)).isChecked());
        Prefs.putBool(this, Prefs.K_NOTIF_OFFLINE,
                ((CheckBox) findViewById(R.id.set_notif_offline)).isChecked());
    }

    private static int clamp(int v, int lo, int hi) {
        return Math.max(lo, Math.min(hi, v));
    }

    // ---- shortcuts ------------------------------------------------------------

    private void refreshShortcutsPreview() {
        try {
            JSONArray arr = new JSONArray(
                    Prefs.getString(this, Prefs.K_SHORTCUTS, "[\"F5\",\"F11\",\"Print\"]"));
            StringBuilder sb = new StringBuilder();
            for (int i = 0; i < arr.length(); i++) {
                if (i > 0) sb.append(", ");
                sb.append(arr.optString(i, ""));
            }
            ((TextView) findViewById(R.id.shortcuts_preview)).setText(sb.toString());
        } catch (Exception ignored) {
        }
    }

    private void editShortcuts() {
        final EditText f = new EditText(this);
        f.setHint("F5, F11, Escape");
        try {
            JSONArray arr = new JSONArray(
                    Prefs.getString(this, Prefs.K_SHORTCUTS, "[\"F5\",\"F11\",\"Print\"]"));
            StringBuilder sb = new StringBuilder();
            for (int i = 0; i < arr.length(); i++) {
                if (i > 0) sb.append(", ");
                sb.append(arr.optString(i, ""));
            }
            f.setText(sb.toString());
        } catch (Exception ignored) {
        }
        int pad = (int) (20 * getResources().getDisplayMetrics().density);
        f.setPadding(pad, pad / 2, pad, pad / 2);
        new AlertDialog.Builder(this)
                .setTitle(R.string.shortcuts_title)
                .setMessage(R.string.shortcuts_hint)
                .setView(f)
                .setPositiveButton(R.string.save, (d, w) -> {
                    try {
                        JSONArray arr = new JSONArray();
                        for (String part : f.getText().toString().split(",")) {
                            String t = part.trim();
                            if (!t.isEmpty()) arr.put(t);
                        }
                        Prefs.putString(this, Prefs.K_SHORTCUTS, arr.toString());
                        refreshShortcutsPreview();
                    } catch (Exception ignored) {
                    }
                })
                .setNegativeButton(R.string.cancel, null)
                .show();
    }

    private void bindAbout() {
        TextView about = findViewById(R.id.about);
        String version = "0.1.0";
        try {
            PackageInfo pi = getPackageManager().getPackageInfo(getPackageName(), 0);
            if (pi.versionName != null) version = pi.versionName;
        } catch (Exception ignored) {
        }
        about.setText(getString(R.string.about, version));
    }
}
