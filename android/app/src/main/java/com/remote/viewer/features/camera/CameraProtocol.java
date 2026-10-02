package com.remote.viewer.features.camera;

/**
 * Wire helpers for the camera-for-verification protocol (REMOTE/4,
 * CAMERA_START/STOP/FRAME/STATUS = 0x87-0x8A).
 *
 * Pure java.lang only -- no Android imports -- so this class is unit
 * tested on a plain JVM (see tests/CameraProtocolTest.java). The JSON
 * is built/parsed by hand to avoid the android.jar org.json stubs.
 *
 * CAMERA_START schema (client -> server):
 *   {"width":1280,"height":720,"fps":30,"facing":"rear",
 *    "rotation":90,"mirror":false,"codec":"h264"}
 * width/height/fps describe the ENCODED stream geometry. rotation is the
 * degrees the host must rotate frames to make them upright (0/90/180/270);
 * mirror is true for front cameras (host applies hflip).
 *
 * CAMERA_STATUS schema (server -> client):
 *   {"active":true,"device":"/dev/video2","width":720,"height":1280,
 *    "fps":30}                      -- stream accepted
 *   {"active":false,"device":"","width":0,"height":0,"fps":0,
 *    "error":"..."}                 -- rejected / failed / stopped
 */
public final class CameraProtocol {

    public static final String CODEC_H264 = "h264";

    private CameraProtocol() {}

    /** Builds the CAMERA_START payload. facing must be "front"|"rear". */
    public static String buildStartJson(int width, int height, int fps,
                                        String facing, int rotation,
                                        boolean mirror) {
        if (!"front".equals(facing) && !"rear".equals(facing)) {
            throw new IllegalArgumentException("facing must be front|rear");
        }
        if (rotation != 0 && rotation != 90 && rotation != 180 && rotation != 270) {
            throw new IllegalArgumentException("rotation must be 0|90|180|270");
        }
        StringBuilder sb = new StringBuilder(128);
        sb.append("{\"width\":").append(width)
          .append(",\"height\":").append(height)
          .append(",\"fps\":").append(fps)
          .append(",\"facing\":\"").append(facing).append('"')
          .append(",\"rotation\":").append(rotation)
          .append(",\"mirror\":").append(mirror)
          .append(",\"codec\":\"").append(CODEC_H264).append("\"}");
        return sb.toString();
    }

    /** Parsed CAMERA_STATUS. */
    public static final class Status {
        public boolean active;
        public String device = "";
        public int width;
        public int height;
        public int fps;
        public String error; // null when absent
    }

    /**
     * Parses a CAMERA_STATUS payload. Throws IllegalArgumentException on
     * malformed input (never returns a half-parsed object).
     */
    public static Status parseStatus(String json) {
        if (json == null) throw new IllegalArgumentException("null status");
        Status s = new Status();
        s.active = readBool(json, "active");
        s.device = readString(json, "device");
        s.width = readInt(json, "width");
        s.height = readInt(json, "height");
        s.fps = readInt(json, "fps");
        s.error = readOptionalString(json, "error");
        return s;
    }

    // ---- minimal flat-JSON reader (schema above only) --------------------

    private static String rawValue(String json, String key) {
        String k = "\"" + key + "\"";
        int i = json.indexOf(k);
        if (i < 0) throw new IllegalArgumentException("missing key: " + key);
        i = json.indexOf(':', i + k.length());
        if (i < 0) throw new IllegalArgumentException("bad json near: " + key);
        i++;
        while (i < json.length() && Character.isWhitespace(json.charAt(i))) i++;
        if (i >= json.length()) throw new IllegalArgumentException("truncated json");
        char c = json.charAt(i);
        if (c == '"') {
            StringBuilder sb = new StringBuilder();
            i++;
            while (i < json.length()) {
                char ch = json.charAt(i);
                if (ch == '\\' && i + 1 < json.length()) {
                    char e = json.charAt(i + 1);
                    if (e == '"') sb.append('"');
                    else if (e == '\\') sb.append('\\');
                    else if (e == 'n') sb.append('\n');
                    else sb.append(e);
                    i += 2;
                } else if (ch == '"') {
                    return sb.toString();
                } else {
                    sb.append(ch);
                    i++;
                }
            }
            throw new IllegalArgumentException("unterminated string: " + key);
        }
        int j = i;
        while (j < json.length() && ",}]".indexOf(json.charAt(j)) < 0) j++;
        String v = json.substring(i, j).trim();
        if (v.isEmpty()) throw new IllegalArgumentException("empty value: " + key);
        return v;
    }

    private static boolean readBool(String json, String key) {
        String v = rawValue(json, key);
        if ("true".equals(v)) return true;
        if ("false".equals(v)) return false;
        throw new IllegalArgumentException("not a bool: " + key + "=" + v);
    }

    private static int readInt(String json, String key) {
        try {
            return Integer.parseInt(rawValue(json, key));
        } catch (NumberFormatException e) {
            throw new IllegalArgumentException("not an int: " + key);
        }
    }

    private static String readString(String json, String key) {
        return rawValue(json, key);
    }

    private static String readOptionalString(String json, String key) {
        String k = "\"" + key + "\"";
        if (json.indexOf(k) < 0) return null;
        return rawValue(json, key);
    }
}
