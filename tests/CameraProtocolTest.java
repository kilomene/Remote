import com.remote.viewer.features.camera.CameraProtocol;

/**
 * Plain-JVM unit test for CameraProtocol (no Android runtime needed:
 * the class is pure java.lang). Run: javac + java from the repo root.
 *
 * Covers: CAMERA_START schema, facing/rotation validation, CAMERA_STATUS
 * parse (accepted / rejected / malformed), and the privacy-relevant
 * guarantee that nothing here can start a camera (protocol only).
 */
public class CameraProtocolTest {
    static int passed = 0;

    static void check(boolean cond, String label) {
        if (!cond) throw new AssertionError("FAIL: " + label);
        passed++;
        System.out.println("ok: " + label);
    }

    public static void main(String[] args) {
        // CAMERA_START schema
        String s = CameraProtocol.buildStartJson(1280, 720, 30, "rear", 90, false);
        check(s.equals("{\"width\":1280,\"height\":720,\"fps\":30,\"facing\":\"rear\","
                + "\"rotation\":90,\"mirror\":false,\"codec\":\"h264\"}"),
                "CAMERA_START json exact schema: " + s);

        String f = CameraProtocol.buildStartJson(640, 480, 15, "front", 270, true);
        check(f.contains("\"facing\":\"front\"") && f.contains("\"rotation\":270")
                && f.contains("\"mirror\":true"), "front/mirror/rotation fields");

        // validation
        try {
            CameraProtocol.buildStartJson(640, 480, 30, "left", 0, false);
            check(false, "bad facing rejected");
        } catch (IllegalArgumentException e) {
            check(true, "bad facing rejected");
        }
        try {
            CameraProtocol.buildStartJson(640, 480, 30, "rear", 45, false);
            check(false, "bad rotation rejected");
        } catch (IllegalArgumentException e) {
            check(true, "bad rotation rejected");
        }

        // CAMERA_STATUS: accepted
        CameraProtocol.Status ok = CameraProtocol.parseStatus(
                "{\"active\":true,\"device\":\"/dev/video2\",\"width\":720,"
                + "\"height\":1280,\"fps\":30}");
        check(ok.active && "/dev/video2".equals(ok.device)
                && ok.width == 720 && ok.height == 1280 && ok.fps == 30
                && ok.error == null, "parse active status");

        // CAMERA_STATUS: rejected with error
        CameraProtocol.Status err = CameraProtocol.parseStatus(
                "{\"active\":false,\"device\":\"\",\"width\":0,\"height\":0,"
                + "\"fps\":0,\"error\":\"v4l2loopback not available\"}");
        check(!err.active && "v4l2loopback not available".equals(err.error),
                "parse rejected status with error");

        // CAMERA_STATUS: error containing escaped quotes
        CameraProtocol.Status esc = CameraProtocol.parseStatus(
                "{\"active\":false,\"device\":\"\",\"width\":0,\"height\":0,"
                + "\"fps\":0,\"error\":\"bad \\\"quoted\\\" detail\"}");
        check("bad \"quoted\" detail".equals(esc.error), "escaped quotes in error");

        // malformed input never yields a half-parsed object
        String[] bad = {null, "", "{}", "{\"active\":true}",
                "{\"active\":\"yes\",\"device\":\"\",\"width\":0,\"height\":0,\"fps\":0}",
                "{\"active\":true,\"device\":\"\",\"width\":\"x\",\"height\":0,\"fps\":0}",
                "{\"active\":true,\"device\":"};
        for (String b : bad) {
            try {
                CameraProtocol.parseStatus(b);
                check(false, "malformed rejected: " + b);
            } catch (IllegalArgumentException e) {
                check(true, "malformed rejected: " + (b == null ? "null" : b));
            }
        }

        System.out.println("CAMERA-PROTOCOL JVM TESTS PASSED (" + passed + " checks)");
    }
}
