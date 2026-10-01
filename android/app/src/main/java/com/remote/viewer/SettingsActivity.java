package com.remote.viewer;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.pm.PackageInfo;
import android.os.Bundle;
import android.widget.ArrayAdapter;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.Spinner;
import android.widget.TextView;
import android.widget.Toast;

/** Viewer settings: haptics, keep-awake, smooth scaling, clipboard sync, fps. */
public class SettingsActivity extends Activity {

    private static final int[] FPS_VALUES = {12, 24, 60};

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_settings);

        Prefs prefs = Prefs.load(this);

        final CheckBox haptics = findViewById(R.id.set_haptics);
        final CheckBox keepAwake = findViewById(R.id.set_keepawake);
        final CheckBox smooth = findViewById(R.id.set_smooth);
        final CheckBox clipboard = findViewById(R.id.set_clipboard);
        final Spinner fps = findViewById(R.id.set_fps);

        haptics.setChecked(prefs.haptics);
        keepAwake.setChecked(prefs.keepAwake);
        smooth.setChecked(prefs.smoothScaling);
        clipboard.setChecked(prefs.clipboard);

        ArrayAdapter<CharSequence> adapter = ArrayAdapter.createFromResource(
                this, R.array.fps_options, android.R.layout.simple_spinner_item);
        adapter.setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item);
        fps.setAdapter(adapter);
        int sel = 2;
        for (int i = 0; i < FPS_VALUES.length; i++) {
            if (FPS_VALUES[i] == prefs.fpsCap) sel = i;
        }
        fps.setSelection(sel);

        android.widget.CompoundButton.OnCheckedChangeListener saver =
                (buttonView, isChecked) -> saveAll();
        haptics.setOnCheckedChangeListener(saver);
        keepAwake.setOnCheckedChangeListener(saver);
        smooth.setOnCheckedChangeListener(saver);
        clipboard.setOnCheckedChangeListener(saver);
        fps.setOnItemSelectedListener(new android.widget.AdapterView.OnItemSelectedListener() {
            @Override public void onItemSelected(android.widget.AdapterView<?> parent,
                                                 android.view.View view, int position, long id) {
                saveAll();
            }
            @Override public void onNothingSelected(android.widget.AdapterView<?> parent) { }
        });

        Button clear = findViewById(R.id.btn_clear);
        clear.setOnClickListener(v -> new AlertDialog.Builder(this)
                .setMessage(R.string.set_clear_sub)
                .setPositiveButton(R.string.set_clear, (d, w) -> {
                    new SecureStore(SettingsActivity.this).clear();
                    Toast.makeText(this, R.string.hosts_cleared, Toast.LENGTH_SHORT).show();
                })
                .setNegativeButton(R.string.cancel, null)
                .show());

        TextView about = findViewById(R.id.about);
        String version = "0.1.0";
        try {
            PackageInfo pi = getPackageManager().getPackageInfo(getPackageName(), 0);
            if (pi.versionName != null) version = pi.versionName;
        } catch (Exception ignored) {
        }
        about.setText(getString(R.string.about, version));
    }

    private void saveAll() {
        CheckBox haptics = findViewById(R.id.set_haptics);
        CheckBox keepAwake = findViewById(R.id.set_keepawake);
        CheckBox smooth = findViewById(R.id.set_smooth);
        CheckBox clipboard = findViewById(R.id.set_clipboard);
        Spinner fps = findViewById(R.id.set_fps);
        int cap = FPS_VALUES[Math.max(0, Math.min(FPS_VALUES.length - 1,
                fps.getSelectedItemPosition()))];
        Prefs.save(this, haptics.isChecked(), keepAwake.isChecked(),
                smooth.isChecked(), clipboard.isChecked(), cap);
    }
}
