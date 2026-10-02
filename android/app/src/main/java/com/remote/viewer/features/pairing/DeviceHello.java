package com.remote.viewer.features.pairing;

import android.content.Context;
import android.os.Build;
import android.provider.Settings;

import com.remote.viewer.RemoteProto;

import org.json.JSONObject;

import java.io.IOException;
import java.net.Socket;
import java.nio.charset.StandardCharsets;

/**
 * v2 device pairing hello (see PROTOCOL.md).
 * Sent once, immediately after TCP connect and before auth. The host records
 * the device and marks it trusted on successful password auth
 * (trust-on-first-use via password).
 */
public final class DeviceHello {
    private DeviceHello() {}

    /** Builds the DEVICE_HELLO JSON payload. */
    public static String buildJson(Context ctx) {
        try {
            String deviceId = Settings.Secure.getString(
                    ctx.getContentResolver(), Settings.Secure.ANDROID_ID);
            if (deviceId == null) deviceId = "unknown";
            String deviceName = (Build.MANUFACTURER + " " + Build.MODEL).trim();
            JSONObject o = new JSONObject();
            o.put("device_id", deviceId);
            o.put("device_name", deviceName);
            o.put("platform", "android");
            return o.toString();
        } catch (Exception e) {
            return "{\"device_id\":\"unknown\",\"device_name\":\"android\",\"platform\":\"android\"}";
        }
    }

    /** Sends DEVICE_HELLO (0x30) on an already-connected socket. */
    public static void send(Context ctx, Socket s) throws IOException {
        byte[] payload = buildJson(ctx).getBytes(StandardCharsets.UTF_8);
        RemoteProto.sendMsg(s.getOutputStream(), RemoteProto.DEVICE_HELLO, payload);
    }
}
