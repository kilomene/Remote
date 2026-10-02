package com.remote.viewer.features.security;

import android.app.Activity;
import android.os.Build;
import android.os.CancellationSignal;

import com.remote.viewer.Prefs;

/**
 * App-lock gate using the framework android.hardware.biometrics.BiometricPrompt
 * (API 29+). The setting is hidden on older devices (no API, no option).
 *
 * Kept in its own class so BiometricPrompt is only referenced on API 29+ —
 * callers must check {@link #isAvailable(Activity)} first.
 */
public final class BiometricLock {

    private BiometricLock() {}

    /** True when the device can actually offer biometric auth: API 29+,
     *  biometric hardware present, and at least one biometric enrolled.
     *  Without this check, authenticate() can throw (and crash the app)
     *  on devices with nothing enrolled. */
    public static boolean isAvailable(Activity activity) {
        if (Build.VERSION.SDK_INT < 29) {
            return false;
        }
        try {
            android.hardware.biometrics.BiometricManager bm =
                    activity.getSystemService(
                            android.hardware.biometrics.BiometricManager.class);
            return bm != null && bm.canAuthenticate()
                    == android.hardware.biometrics.BiometricManager.BIOMETRIC_SUCCESS;
        } catch (Exception e) {
            return false;
        }
    }

    public interface Callback {
        void onAuthenticated();
        void onFailed();
    }

    /**
     * Runs the biometric prompt. Calls back onAuthenticated() on success,
     * onFailed() on error / negative button / no biometrics enrolled.
     * Never throws: any failure to show the prompt reports onFailed()
     * instead of crashing the app.
     */
    public static void authenticate(final Activity activity, final Callback cb) {
        if (!isAvailable(activity)) {
            cb.onFailed();
            return;
        }
        try {
            BiometricPromptHost.prompt(activity, cb);
        } catch (Exception e) {
            cb.onFailed();
        }
    }

    /** True when the user enabled the lock and the device supports it. */
    public static boolean isLockEnabled(Activity activity) {
        return isAvailable(activity)
                && Prefs.getBool(activity, Prefs.K_BIOMETRIC, false);
    }

    /**
     * Inner host class: holds every direct reference to
     * android.hardware.biometrics.BiometricPrompt so the outer class stays
     * clean on pre-29 runtimes.
     */
    private static final class BiometricPromptHost {
        static void prompt(final Activity activity, final Callback cb) {
            android.hardware.biometrics.BiometricPrompt.Builder builder =
                    new android.hardware.biometrics.BiometricPrompt.Builder(activity)
                            .setTitle("Unlock Remote")
                            .setSubtitle("Authenticate to open the app")
                            .setNegativeButton("Cancel",
                                    activity.getMainExecutor(),
                                    (dialog, which) -> cb.onFailed());
            android.hardware.biometrics.BiometricPrompt prompt = builder.build();
            final CancellationSignal cancel = new CancellationSignal();
            prompt.authenticate(cancel, activity.getMainExecutor(),
                    new android.hardware.biometrics.BiometricPrompt.AuthenticationCallback() {
                        @Override
                        public void onAuthenticationSucceeded(
                                android.hardware.biometrics.BiometricPrompt.AuthenticationResult result) {
                            cb.onAuthenticated();
                        }

                        @Override
                        public void onAuthenticationError(int errorCode, CharSequence errString) {
                            cb.onFailed();
                        }

                        @Override
                        public void onAuthenticationFailed() {
                            // single failed attempt: keep the prompt open
                        }
                    });
        }
    }
}
