package com.remote.viewer;

import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;
import java.security.KeyStore;
import java.util.ArrayList;
import java.util.List;

import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

import org.json.JSONArray;
import org.json.JSONObject;

/**
 * Saved hosts (including passwords), encrypted with an AES-256-GCM key held
 * in the AndroidKeyStore. Native equivalent of EncryptedSharedPreferences —
 * no extra dependencies.
 */
public class SecureStore {

    private static final String KEY_ALIAS = "RemoteViewerHosts";
    private static final String FILE_NAME = "hosts.enc";
    private static final int GCM_TAG_BITS = 128;

    public static class Host {
        public final String name;
        public final String host;
        public final int port;
        public final String password;
        public final boolean favorite;
        public final String tags;
        public final int icon;

        public Host(String name, String host, int port, String password) {
            this(name, host, port, password, false, "", 0);
        }

        public Host(String name, String host, int port, String password, boolean favorite) {
            this(name, host, port, password, favorite, "", 0);
        }

        public Host(String name, String host, int port, String password,
                    boolean favorite, String tags, int icon) {
            this.name = name;
            this.host = host;
            this.port = port;
            this.password = password;
            this.favorite = favorite;
            this.tags = tags == null ? "" : tags;
            this.icon = icon;
        }
    }

    private final File file;

    public SecureStore(android.content.Context ctx) {
        file = new File(ctx.getFilesDir(), FILE_NAME);
    }

    private SecretKey getKey() throws Exception {
        KeyStore ks = KeyStore.getInstance("AndroidKeyStore");
        ks.load(null);
        if (ks.containsAlias(KEY_ALIAS)) {
            return (SecretKey) ks.getKey(KEY_ALIAS, null);
        }
        KeyGenerator kg = KeyGenerator.getInstance(
                KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore");
        kg.init(new KeyGenParameterSpec.Builder(KEY_ALIAS,
                KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setRandomizedEncryptionRequired(true)
                .build());
        return kg.generateKey();
    }

    public synchronized List<Host> load() {
        List<Host> out = new ArrayList<Host>();
        if (!file.exists()) return out;
        try {
            byte[] blob = readAll(file);
            if (blob.length < 13) return out;
            byte[] iv = new byte[12];
            System.arraycopy(blob, 0, iv, 0, 12);
            byte[] ct = new byte[blob.length - 12];
            System.arraycopy(blob, 12, ct, 0, ct.length);
            Cipher c = Cipher.getInstance("AES/GCM/NoPadding");
            c.init(Cipher.DECRYPT_MODE, getKey(), new GCMParameterSpec(GCM_TAG_BITS, iv));
            String json = new String(c.doFinal(ct), StandardCharsets.UTF_8);
            JSONArray arr = new JSONArray(json);
            for (int i = 0; i < arr.length(); i++) {
                JSONObject o = arr.getJSONObject(i);
                out.add(new Host(o.getString("name"), o.getString("host"),
                        o.getInt("port"), o.optString("password", ""),
                        o.optBoolean("favorite", false),
                        o.optString("tags", ""), o.optInt("icon", 0)));
            }
        } catch (Exception ignored) {
            // Corrupt store: start fresh rather than crash.
        }
        return out;
    }

    public synchronized void save(List<Host> hosts) {
        try {
            JSONArray arr = new JSONArray();
            for (Host h : hosts) {
                JSONObject o = new JSONObject();
                o.put("name", h.name);
                o.put("host", h.host);
                o.put("port", h.port);
                o.put("password", h.password);
                o.put("favorite", h.favorite);
                o.put("tags", h.tags);
                o.put("icon", h.icon);
                arr.put(o);
            }
            byte[] pt = arr.toString().getBytes(StandardCharsets.UTF_8);
            Cipher c = Cipher.getInstance("AES/GCM/NoPadding");
            c.init(Cipher.ENCRYPT_MODE, getKey());
            byte[] iv = c.getIV();
            byte[] ct = c.doFinal(pt);
            ByteArrayOutputStream bos = new ByteArrayOutputStream();
            bos.write(iv);
            bos.write(ct);
            FileOutputStream fos = new FileOutputStream(file);
            try {
                fos.write(bos.toByteArray());
            } finally {
                fos.close();
            }
        } catch (Exception ignored) {
        }
    }

    public synchronized void upsert(Host host) {
        List<Host> hosts = load();
        for (int i = 0; i < hosts.size(); i++) {
            Host h = hosts.get(i);
            if (h.host.equals(host.host) && h.port == host.port) {
                hosts.set(i, host);
                save(hosts);
                return;
            }
        }
        hosts.add(0, host);
        save(hosts);
    }

    public synchronized void delete(Host host) {
        List<Host> hosts = load();
        for (int i = 0; i < hosts.size(); i++) {
            Host h = hosts.get(i);
            if (h.host.equals(host.host) && h.port == host.port) {
                hosts.remove(i);
                break;
            }
        }
        save(hosts);
    }

    public synchronized void clear() {
        if (file.exists()) file.delete();
        try {
            KeyStore ks = KeyStore.getInstance("AndroidKeyStore");
            ks.load(null);
            if (ks.containsAlias(KEY_ALIAS)) ks.deleteEntry(KEY_ALIAS);
        } catch (Exception ignored) {
        }
    }

    private static byte[] readAll(File f) throws Exception {
        FileInputStream fis = new FileInputStream(f);
        try {
            ByteArrayOutputStream bos = new ByteArrayOutputStream();
            byte[] tmp = new byte[4096];
            int r;
            while ((r = fis.read(tmp)) > 0) bos.write(tmp, 0, r);
            return bos.toByteArray();
        } finally {
            fis.close();
        }
    }

    @SuppressWarnings("unused")
    private static String b64(byte[] b) {
        return Base64.encodeToString(b, Base64.NO_WRAP);
    }
}
