package com.remote.viewer.features.camera;

import android.content.Context;
import android.hardware.camera2.CameraAccessException;
import android.hardware.camera2.CameraCaptureSession;
import android.hardware.camera2.CameraCharacteristics;
import android.hardware.camera2.CameraDevice;
import android.hardware.camera2.CameraManager;
import android.hardware.camera2.CaptureRequest;
import android.hardware.camera2.params.StreamConfigurationMap;
import android.media.MediaCodec;
import android.media.MediaCodecInfo;
import android.media.MediaCodecList;
import android.media.MediaFormat;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.HandlerThread;
import android.util.Range;
import android.view.Surface;
import android.view.WindowManager;

import java.nio.ByteBuffer;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * "Camera for Verification" capture engine: Android Camera2 -> hardware
 * H.264 (MediaCodec, surface input) -> Annex-B access units -> caller sink.
 *
 * Privacy contract (enforced by the caller, SessionActivity):
 *  - start() is only ever called from an explicit user tap. There is no
 *    auto-start, no background capture, no recording to storage.
 *  - stop() tears everything down: repeating request, capture session,
 *    camera device, encoder, threads. Idempotent, safe from any thread.
 *
 * Pipeline threads:
 *  - "CameraStream" HandlerThread: all Camera2 callbacks.
 *  - "CameraDrain": MediaCodec.dequeueOutputBuffer loop -> builds one
 *    Annex-B access unit per buffer -> bounded queue (drop-oldest).
 *  - "CameraSender": takes access units -> FrameSink. Network stalls can
 *    never grow memory: the queue is capped and the encoder keeps running.
 *
 * Encoder output contract (matches PROTOCOL.md, verified against the real
 * ffmpeg decoder in tests/proto_v4_camera_decode.py):
 *  - Annex-B byte stream; one access unit per sink call.
 *  - SPS/PPS (from csd-0/csd-1 or codec-config buffers, start-code
 *    normalized) are prepended to EVERY IDR, so the first frame the host
 *    sees is always self-contained and the host needs no decoder state.
 */
public class CameraStreamer {

    private static final String MIME = MediaFormat.MIMETYPE_VIDEO_AVC;
    private static final int TARGET_W = 1280;
    private static final int TARGET_H = 720;
    private static final int TARGET_FPS = 30;
    private static final int BITRATE = 1_500_000;
    private static final int IFRAME_SEC = 2;
    private static final int QUEUE_CAP = 4;
    private static final byte[] START_CODE = {0, 0, 0, 1};

    /** One Annex-B access unit (SPS/PPS-prefixed when a keyframe). */
    public interface FrameSink {
        void onAccessUnit(byte[] data);
    }

    public interface Callback {
        void onStarted(int width, int height, int fps, String facing);
        void onError(String reason);
        void onStopped();
    }

    /** Available camera: id plus front/rear facing. */
    public static final class CameraInfo {
        public final String id;
        public final String facing; // "front" | "rear"
        public final int sensorOrientation;

        CameraInfo(String id, String facing, int sensorOrientation) {
            this.id = id;
            this.facing = facing;
            this.sensorOrientation = sensorOrientation;
        }
    }

    // ---- static capability queries (used before start, e.g. to build
    // ---- CAMERA_START while awaiting the host's CAMERA_STATUS) ----------

    /** All cameras, front/rear labeled. Empty list = no camera. */
    public static List<CameraInfo> enumerate(Context ctx) {
        List<CameraInfo> out = new ArrayList<CameraInfo>();
        CameraManager cm = (CameraManager) ctx.getSystemService(Context.CAMERA_SERVICE);
        if (cm == null) return out;
        try {
            for (String id : cm.getCameraIdList()) {
                CameraCharacteristics c = cm.getCameraCharacteristics(id);
                Integer lens = c.get(CameraCharacteristics.LENS_FACING);
                String facing = (lens != null
                        && lens == CameraCharacteristics.LENS_FACING_FRONT)
                        ? "front" : "rear";
                Integer orient = c.get(CameraCharacteristics.SENSOR_ORIENTATION);
                out.add(new CameraInfo(id, facing, orient == null ? 0 : orient));
            }
        } catch (CameraAccessException ignored) {
        }
        return out;
    }

    /** First camera matching facing, or null. */
    public static CameraInfo findCamera(Context ctx, String facing) {
        for (CameraInfo ci : enumerate(ctx)) {
            if (ci.facing.equals(facing)) return ci;
        }
        return null;
    }

    /**
     * Encoder size for a camera: the supported MediaCodec size closest to
     * 1280x720 (never assumed to exist). Returns {w, h}, both even.
     */
    public static int[] chooseSize(Context ctx, String cameraId)
            throws CameraAccessException {
        CameraManager cm = (CameraManager) ctx.getSystemService(Context.CAMERA_SERVICE);
        CameraCharacteristics c = cm.getCameraCharacteristics(cameraId);
        StreamConfigurationMap map = c.get(
                CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP);
        android.util.Size[] sizes = null;
        if (map != null) {
            sizes = map.getOutputSizes(MediaCodec.class);
            if (sizes == null || sizes.length == 0) {
                sizes = map.getOutputSizes(android.graphics.SurfaceTexture.class);
            }
        }
        if (sizes == null || sizes.length == 0) {
            throw new CameraAccessException(CameraAccessException.CAMERA_ERROR,
                    "camera has no usable output sizes");
        }
        android.util.Size best = sizes[0];
        long bestScore = Long.MAX_VALUE;
        long target = (long) TARGET_W * TARGET_H;
        for (android.util.Size s : sizes) {
            long score = Math.abs((long) s.getWidth() * s.getHeight() - target);
            // prefer landscape 16:9-ish candidates on ties
            if (score < bestScore) {
                bestScore = score;
                best = s;
            }
        }
        int w = Math.max(2, best.getWidth() & ~1);
        int h = Math.max(2, best.getHeight() & ~1);
        return new int[]{w, h};
    }

    /**
     * Frames per second: the AE target range containing 30fps, else the
     * highest available ceiling, clamped to [15, 30].
     */
    public static int chooseFps(Context ctx, String cameraId)
            throws CameraAccessException {
        CameraManager cm = (CameraManager) ctx.getSystemService(Context.CAMERA_SERVICE);
        CameraCharacteristics c = cm.getCameraCharacteristics(cameraId);
        Range<Integer>[] ranges = c.get(
                CameraCharacteristics.CONTROL_AE_AVAILABLE_TARGET_FPS_RANGES);
        if (ranges == null || ranges.length == 0) return 15;
        for (Range<Integer> r : ranges) {
            if (r.getLower() <= TARGET_FPS && r.getUpper() >= TARGET_FPS) {
                return TARGET_FPS;
            }
        }
        int hi = 0;
        for (Range<Integer> r : ranges) hi = Math.max(hi, r.getUpper());
        return Math.max(15, Math.min(TARGET_FPS, hi));
    }

    /** AE fps range to request for the chosen fps. */
    private static Range<Integer> fpsRangeFor(Context ctx, String cameraId, int fps)
            throws CameraAccessException {
        CameraManager cm = (CameraManager) ctx.getSystemService(Context.CAMERA_SERVICE);
        CameraCharacteristics c = cm.getCameraCharacteristics(cameraId);
        Range<Integer>[] ranges = c.get(
                CameraCharacteristics.CONTROL_AE_AVAILABLE_TARGET_FPS_RANGES);
        Range<Integer> best = null;
        if (ranges != null) {
            for (Range<Integer> r : ranges) {
                if (r.getLower() <= fps && r.getUpper() >= fps) {
                    if (best == null || r.getUpper() < best.getUpper()) best = r;
                }
            }
            if (best == null) {
                for (Range<Integer> r : ranges) {
                    if (best == null || r.getUpper() > best.getUpper()) best = r;
                }
            }
        }
        return best;
    }

    /**
     * Degrees the host must rotate frames to make them upright, from the
     * canonical Camera2 formula (sensor orientation vs current display
     * rotation, mirrored for front cameras). One of 0/90/180/270.
     */
    public static int computeRotation(Context ctx, CameraInfo info) {
        WindowManager wm = (WindowManager) ctx.getSystemService(Context.WINDOW_SERVICE);
        int displayRotation = 0;
        if (wm != null && wm.getDefaultDisplay() != null) {
            switch (wm.getDefaultDisplay().getRotation()) {
                case Surface.ROTATION_90: displayRotation = 90; break;
                case Surface.ROTATION_180: displayRotation = 180; break;
                case Surface.ROTATION_270: displayRotation = 270; break;
                default: displayRotation = 0;
            }
        }
        int deviceOrientation = "front".equals(info.facing)
                ? -displayRotation : displayRotation;
        return (info.sensorOrientation + deviceOrientation + 360) % 360;
    }

    // ---- instance ------------------------------------------------------

    private final AtomicBoolean running = new AtomicBoolean(false);
    private final AtomicBoolean startFailed = new AtomicBoolean(false);
    private final AtomicBoolean tornDown = new AtomicBoolean(false);
    // Start/stop generation: a teardown posted by stop() must never kill a
    // session started afterwards. Guarded by synchronizing start()/stop().
    private long generation;

    private HandlerThread camThread;
    private Handler camHandler;
    private Thread drainThread;
    private Thread senderThread;
    private final BlockingQueue<byte[]> frameQueue =
            new ArrayBlockingQueue<byte[]>(QUEUE_CAP);

    private MediaCodec codec;
    private Surface encoderSurface;
    private CameraDevice cameraDevice;
    private CameraCaptureSession captureSession;

    private volatile FrameSink sink;
    private volatile Callback callback;
    private volatile String facing = "rear";
    private volatile int width;
    private volatile int height;
    private volatile int fps;
    private volatile byte[] spsPps; // start-code-prefixed SPS+PPS
    private volatile long droppedFrames;
    private volatile long lastKeyframeReqMs;

    public boolean isStreaming() {
        return running.get();
    }

    public int getWidth() { return width; }
    public int getHeight() { return height; }
    public int getFps() { return fps; }
    public String getFacing() { return facing; }
    public long getDroppedFrames() { return droppedFrames; }

    /**
     * Starts capture on the given facing. All work happens off the caller
     * thread; results arrive on callback (which may be any thread --
     * the caller must hop to UI itself).
     */
    public synchronized void start(final Context ctx, final String facing,
                                     final FrameSink sink, final Callback callback) {
        if (!running.compareAndSet(false, true)) return; // already running
        final long gen = ++generation;
        this.sink = sink;
        this.callback = callback;
        this.facing = facing;
        this.spsPps = null;
        this.droppedFrames = 0;
        this.lastKeyframeReqMs = 0;
        startFailed.set(false);
        tornDown.set(false);
        frameQueue.clear();

        camThread = new HandlerThread("CameraStream");
        camThread.start();
        camHandler = new Handler(camThread.getLooper());
        camHandler.post(new Runnable() {
            @Override public void run() {
                if (gen != generation) return; // superseded by a later stop/start
                try {
                    startOnCamThread(ctx.getApplicationContext(), gen);
                } catch (Exception e) {
                    fail(e.getMessage() == null ? "camera start failed"
                            : e.getMessage());
                }
            }
        });
    }

    /** Idempotent: safe to call any number of times, from any thread. */
    public synchronized void stop() {
        if (!running.compareAndSet(true, false)) return;
        final long gen = ++generation;
        Handler h = camHandler;
        if (h != null) {
            h.post(new Runnable() {
                @Override public void run() {
                    teardown(gen);
                }
            });
        } else {
            teardown(gen);
        }
    }

    // ---- internals (camThread unless noted) ----------------------------

    private void fail(String reason) {
        if (startFailed.compareAndSet(false, true)) {
            final Callback cb = callback;
            running.set(false);
            teardown(generation);
            if (cb != null) cb.onError(reason);
        }
    }

    private void startOnCamThread(Context ctx, long gen) throws Exception {
        CameraInfo info = findCamera(ctx, facing);
        if (info == null) {
            // requested facing missing: report honestly, don't silently
            // substitute the other camera for a verification feature
            throw new Exception("no " + facing + " camera on this device");
        }
        int[] wh = chooseSize(ctx, info.id);
        width = wh[0];
        height = wh[1];
        fps = chooseFps(ctx, info.id);

        codec = createAvcEncoder();
        MediaFormat format = MediaFormat.createVideoFormat(MIME, width, height);
        format.setInteger(MediaFormat.KEY_COLOR_FORMAT,
                MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface);
        format.setInteger(MediaFormat.KEY_BIT_RATE, BITRATE);
        format.setInteger(MediaFormat.KEY_FRAME_RATE, fps);
        format.setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, IFRAME_SEC);
        try {
            format.setInteger(MediaFormat.KEY_PROFILE,
                    MediaCodecInfo.CodecProfileLevel.AVCProfileBaseline);
            format.setInteger(MediaFormat.KEY_LEVEL,
                    MediaCodecInfo.CodecProfileLevel.AVCLevel31);
        } catch (Exception ignored) {
            // profile/level keys are advisory; drop them on picky devices
        }
        try {
            codec.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE);
        } catch (Exception e) {
            // retry without the advisory profile/level keys
            format = MediaFormat.createVideoFormat(MIME, width, height);
            format.setInteger(MediaFormat.KEY_COLOR_FORMAT,
                    MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface);
            format.setInteger(MediaFormat.KEY_BIT_RATE, BITRATE);
            format.setInteger(MediaFormat.KEY_FRAME_RATE, fps);
            format.setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, IFRAME_SEC);
            codec.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE);
        }
        encoderSurface = codec.createInputSurface();
        codec.start();

        startDrainThread();
        startSenderThread();

        CameraManager cm = (CameraManager) ctx.getSystemService(Context.CAMERA_SERVICE);
        final Range<Integer> fpsRange = fpsRangeFor(ctx, info.id, fps);
        final String camId = info.id;
        try {
            cm.openCamera(camId, new CameraDevice.StateCallback() {
                @Override public void onOpened(CameraDevice device) {
                    if (!running.get()) {
                        device.close();
                        return;
                    }
                    cameraDevice = device;
                    createSession(fpsRange);
                }
                @Override public void onDisconnected(CameraDevice device) {
                    device.close();
                    if (running.get()) fail("camera disconnected");
                }
                @Override public void onError(CameraDevice device, int error) {
                    device.close();
                    if (running.get()) fail("camera device error " + error);
                }
            }, camHandler);
        } catch (SecurityException e) {
            throw new Exception("camera permission revoked");
        }
        // request an IDR immediately so the first frames decode standalone
        requestKeyframe();
    }

    /** Prefer a hardware AVC encoder; fall back to software; fail honestly. */
    private MediaCodec createAvcEncoder() throws Exception {
        MediaCodecList list = new MediaCodecList(MediaCodecList.ALL_CODECS);
        String software = null;
        for (MediaCodecInfo info : list.getCodecInfos()) {
            if (!info.isEncoder()) continue;
            boolean avc = false;
            for (String t : info.getSupportedTypes()) {
                if (MIME.equalsIgnoreCase(t)) { avc = true; break; }
            }
            if (!avc) continue;
            String name = info.getName();
            if (name.startsWith("OMX.google.")) {
                if (software == null) software = name; // software fallback
            } else {
                return MediaCodec.createByCodecName(name); // hardware
            }
        }
        if (software != null) return MediaCodec.createByCodecName(software);
        // last resort: framework default (may itself pick software)
        return MediaCodec.createEncoderByType(MIME);
    }

    private void createSession(final Range<Integer> fpsRange) {
        try {
            cameraDevice.createCaptureSession(
                    Collections.singletonList(encoderSurface),
                    new CameraCaptureSession.StateCallback() {
                        @Override public void onConfigured(CameraCaptureSession session) {
                            if (!running.get()) {
                                session.close();
                                return;
                            }
                            captureSession = session;
                            try {
                                CaptureRequest.Builder b = cameraDevice
                                        .createCaptureRequest(CameraDevice.TEMPLATE_RECORD);
                                b.addTarget(encoderSurface);
                                b.set(CaptureRequest.CONTROL_MODE,
                                        CaptureRequest.CONTROL_MODE_AUTO);
                                if (fpsRange != null) {
                                    b.set(CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE,
                                            fpsRange);
                                }
                                session.setRepeatingRequest(b.build(), null, camHandler);
                                Callback cb = callback;
                                if (cb != null) {
                                    cb.onStarted(width, height, fps, facing);
                                }
                            } catch (CameraAccessException e) {
                                fail("failed to start capture: " + e.getMessage());
                            }
                        }
                        @Override public void onConfigureFailed(CameraCaptureSession session) {
                            if (running.get()) fail("camera session configuration failed");
                        }
                    }, camHandler);
        } catch (CameraAccessException e) {
            fail("failed to create capture session: " + e.getMessage());
        }
    }

    private void startDrainThread() {
        drainThread = new Thread(new Runnable() {
            @Override public void run() {
                drainLoop();
            }
        }, "CameraDrain");
        drainThread.setDaemon(true);
        drainThread.start();
    }

    private void startSenderThread() {
        senderThread = new Thread(new Runnable() {
            @Override public void run() {
                try {
                    while (running.get() || !frameQueue.isEmpty()) {
                        byte[] au = frameQueue.poll(200,
                                java.util.concurrent.TimeUnit.MILLISECONDS);
                        FrameSink s = sink;
                        if (au != null && s != null) {
                            try {
                                s.onAccessUnit(au);
                            } catch (Exception ignored) {
                            }
                        }
                    }
                } catch (InterruptedException ignored) {
                }
            }
        }, "CameraSender");
        senderThread.setDaemon(true);
        senderThread.start();
    }

    private void drainLoop() {
        MediaCodec.BufferInfo info = new MediaCodec.BufferInfo();
        MediaCodec c = codec;
        while (running.get() && c != null) {
            int idx;
            try {
                idx = c.dequeueOutputBuffer(info, 10000);
            } catch (Exception e) {
                if (running.get()) fail("encoder error: " + e.getMessage());
                return;
            }
            if (idx == MediaCodec.INFO_TRY_AGAIN_LATER) {
                continue;
            } else if (idx == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
                stashCsdFromFormat(c.getOutputFormat());
            } else if (idx >= 0) {
                try {
                    boolean config = (info.flags
                            & MediaCodec.BUFFER_FLAG_CODEC_CONFIG) != 0;
                    boolean key = (info.flags
                            & MediaCodec.BUFFER_FLAG_KEY_FRAME) != 0;
                    if (config) {
                        stashCsdFromBuffer(c, idx, info);
                    } else if (info.size > 0) {
                        byte[] au = copyOutput(c, idx, info);
                        if (key && spsPps != null) au = concat(spsPps, au);
                        offer(au);
                    }
                } finally {
                    try {
                        c.releaseOutputBuffer(idx, false);
                    } catch (Exception ignored) {
                    }
                }
                if ((info.flags & MediaCodec.BUFFER_FLAG_END_OF_STREAM) != 0) return;
            }
        }
    }

    /** Bounded handoff: drop-oldest when the network is slower than the camera. */
    private void offer(byte[] au) {
        if (!frameQueue.offer(au)) {
            frameQueue.poll();
            frameQueue.offer(au);
            droppedFrames++;
            requestKeyframe(); // dropped frames may have eaten the last IDR
        }
    }

    /** Ask the encoder for an IDR soon (throttled to one per 2s). */
    private void requestKeyframe() {
        MediaCodec c = codec;
        if (c == null) return;
        long now = System.currentTimeMillis();
        if (now - lastKeyframeReqMs < 2000) return;
        lastKeyframeReqMs = now;
        try {
            Bundle b = new Bundle();
            b.putInt("request-sync", 0); // PARAMETER_KEY_REQUEST_SYNC_FRAME
            c.setParameters(b);
        } catch (Exception ignored) {
        }
    }

    private void stashCsdFromFormat(MediaFormat format) {
        try {
            ByteBuffer sps = format.getByteBuffer("csd-0");
            ByteBuffer pps = format.getByteBuffer("csd-1");
            if (sps != null && pps != null) {
                spsPps = concat(withStartCode(sps), withStartCode(pps));
            }
        } catch (Exception ignored) {
        }
    }

    private void stashCsdFromBuffer(MediaCodec c, int idx, MediaCodec.BufferInfo info) {
        try {
            ByteBuffer buf = c.getOutputBuffer(idx);
            if (buf == null || info.size <= 0) return;
            byte[] raw = new byte[info.size];
            buf.position(info.offset);
            buf.get(raw);
            if (startsWithStartCode(raw)) spsPps = raw;
        } catch (Exception ignored) {
        }
    }

    private byte[] copyOutput(MediaCodec c, int idx, MediaCodec.BufferInfo info) {
        ByteBuffer buf = c.getOutputBuffer(idx);
        byte[] out = new byte[info.size];
        buf.position(info.offset);
        buf.get(out);
        return out;
    }

    private static boolean startsWithStartCode(byte[] d) {
        return d.length >= 4 && d[0] == 0 && d[1] == 0 && d[2] == 0 && d[3] == 1;
    }

    private static byte[] withStartCode(ByteBuffer buf) {
        ByteBuffer dup = buf.duplicate();
        byte[] raw = new byte[dup.remaining()];
        dup.get(raw);
        if (startsWithStartCode(raw)) return raw;
        // also accept 3-byte start codes already present
        if (raw.length >= 3 && raw[0] == 0 && raw[1] == 0 && raw[2] == 1) return raw;
        return concat(START_CODE, raw);
    }

    private static byte[] concat(byte[] a, byte[] b) {
        byte[] out = new byte[a.length + b.length];
        System.arraycopy(a, 0, out, 0, a.length);
        System.arraycopy(b, 0, out, a.length, b.length);
        return out;
    }

    private void teardown(long gen) {
        if (gen != generation) return; // stale: a newer start/stop owns this
        if (!tornDown.compareAndSet(false, true)) return; // never twice
        Thread self = Thread.currentThread();
        // Stop the repeating request and close camera resources first so no
        // new frames arrive while the encoder drains.
        CameraCaptureSession session = captureSession;
        captureSession = null;
        if (session != null) {
            try { session.stopRepeating(); } catch (Exception ignored) {}
            try { session.close(); } catch (Exception ignored) {}
        }
        CameraDevice device = cameraDevice;
        cameraDevice = null;
        if (device != null) {
            try { device.close(); } catch (Exception ignored) {}
        }
        // halt the drain + sender threads, then release the encoder
        Thread dt = drainThread;
        drainThread = null;
        Thread st = senderThread;
        senderThread = null;
        frameQueue.clear();
        if (dt != null && dt != self) {
            try { dt.join(2000); } catch (InterruptedException ignored) {}
        }
        if (st != null && st != self) {
            st.interrupt();
            try { st.join(2000); } catch (InterruptedException ignored) {}
        }
        MediaCodec c = codec;
        codec = null;
        if (c != null) {
            try { c.stop(); } catch (Exception ignored) {}
            try { c.release(); } catch (Exception ignored) {}
        }
        Surface s = encoderSurface;
        encoderSurface = null;
        if (s != null) {
            try { s.release(); } catch (Exception ignored) {}
        }
        HandlerThread ht = camThread;
        camThread = null;
        camHandler = null;
        if (ht != null) ht.quitSafely();
        sink = null;
        Callback cb = callback;
        callback = null;
        if (cb != null && !startFailed.get()) {
            try { cb.onStopped(); } catch (Exception ignored) {}
        }
    }
}
