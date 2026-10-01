package com.remote.viewer.features.recording;

import android.graphics.Bitmap;
import android.media.MediaCodec;
import android.media.MediaCodecInfo;
import android.media.MediaFormat;
import android.media.MediaMuxer;
import android.os.Build;

import java.io.File;
import java.nio.ByteBuffer;

/**
 * Client-side screen recorder: MediaCodec AVC encoder fed with NV12 frames
 * converted from decoded Bitmaps, muxed to MP4 via MediaMuxer into
 * Movies/Remote. Modest target (default 640x360 or source/2, 15fps) to stay
 * cheap on CPU.
 *
 * Lifecycle: start(file, w, h, fps) -> feedFrame(bitmap)* (drops when the
 * encoder is busy) -> pause()/resume() -> stop() returns the file.
 * All calls from a single caller thread; stop() blocks until drained.
 */
public class ScreenRecorder {

    private static final String MIME = MediaFormat.MIMETYPE_VIDEO_AVC;
    private static final int BITRATE = 2_000_000;
    private static final int IFRAME = 2; // seconds between I-frames

    private MediaCodec codec;
    private MediaMuxer muxer;
    private int track = -1;
    private boolean muxerStarted;
    private boolean paused;
    private boolean stopped;
    private int width;
    private int height;
    private long frameIndex;
    private byte[] frameBuf;
    private final File outFile;

    public ScreenRecorder(File outFile) {
        this.outFile = outFile;
    }

    public synchronized void start(int srcW, int srcH, int fps) throws Exception {
        if (codec != null) throw new IllegalStateException("already started");
        width = round2(Math.min(srcW / 2, 640));
        height = round2(Math.min(srcH / 2, 360));
        if (width < 160) width = round2(srcW);
        if (height < 120) height = round2(srcH);
        frameBuf = new byte[width * height * 3 / 2];

        MediaFormat format = MediaFormat.createVideoFormat(MIME, width, height);
        format.setInteger(MediaFormat.KEY_COLOR_FORMAT,
                MediaCodecInfo.CodecCapabilities.COLOR_FormatYUV420SemiPlanar);
        format.setInteger(MediaFormat.KEY_BIT_RATE, BITRATE);
        format.setInteger(MediaFormat.KEY_FRAME_RATE, Math.max(1, fps));
        format.setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, IFRAME);
        codec = MediaCodec.createEncoderByType(MIME);
        codec.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE);
        codec.start();

        File parent = outFile.getParentFile();
        if (parent != null && !parent.exists()) parent.mkdirs();
        int outFormat = MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4;
        muxer = new MediaMuxer(outFile.getAbsolutePath(), outFormat);
        muxerStarted = false;
        frameIndex = 0;
        paused = false;
        stopped = false;
    }

    /** Feeds one frame; drops it if the encoder has no free input buffer. */
    public synchronized void feedFrame(Bitmap bmp) {
        if (codec == null || stopped || paused || bmp == null || bmp.isRecycled()) return;
        Bitmap scaled = bmp;
        Bitmap tmp = null;
        if (bmp.getWidth() != width || bmp.getHeight() != height) {
            tmp = Bitmap.createScaledBitmap(bmp, width, height, true);
            scaled = tmp;
        }
        try {
            argbToNv12(scaled, frameBuf, width, height);
        } finally {
            if (tmp != null) tmp.recycle();
        }
        try {
            int inIdx = codec.dequeueInputBuffer(0);
            if (inIdx < 0) return; // encoder busy: drop, don't queue
            ByteBuffer in = codec.getInputBuffer(inIdx);
            if (in == null) return;
            in.clear();
            in.put(frameBuf);
            long ptsUs = frameIndex * 1_000_000L / 15L;
            frameIndex++;
            codec.queueInputBuffer(inIdx, 0, frameBuf.length, ptsUs, 0);
            drain(false);
        } catch (Exception ignored) {
        }
    }

    public synchronized void pause() {
        paused = true;
    }

    public synchronized void resume() {
        paused = false;
    }

    public synchronized boolean isPaused() {
        return paused;
    }

    /** Stops the encoder, drains it and finalizes the MP4. Returns the file. */
    public synchronized File stop() {
        stopped = true;
        try {
            if (codec != null) {
                int inIdx = codec.dequeueInputBuffer(10000);
                if (inIdx >= 0) {
                    codec.queueInputBuffer(inIdx, 0, 0, 0,
                            MediaCodec.BUFFER_FLAG_END_OF_STREAM);
                }
                drain(true);
                codec.stop();
                codec.release();
            }
        } catch (Exception ignored) {
        } finally {
            codec = null;
        }
        try {
            if (muxer != null) {
                if (muxerStarted) muxer.stop();
                muxer.release();
            }
        } catch (Exception ignored) {
        } finally {
            muxer = null;
        }
        return outFile;
    }

    private void drain(boolean endOfStream) {
        MediaCodec.BufferInfo info = new MediaCodec.BufferInfo();
        while (true) {
            int outIdx = codec.dequeueOutputBuffer(info, 10000);
            if (outIdx == MediaCodec.INFO_TRY_AGAIN_LATER) {
                if (!endOfStream) break;
            } else if (outIdx == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
                if (!muxerStarted) {
                    track = muxer.addTrack(codec.getOutputFormat());
                    muxer.start();
                    muxerStarted = true;
                }
            } else if (outIdx >= 0) {
                ByteBuffer encoded = codec.getOutputBuffer(outIdx);
                if (encoded != null && (info.flags & MediaCodec.BUFFER_FLAG_CODEC_CONFIG) == 0
                        && muxerStarted && info.size > 0) {
                    encoded.position(info.offset);
                    encoded.limit(info.offset + info.size);
                    muxer.writeSampleData(track, encoded, info);
                }
                codec.releaseOutputBuffer(outIdx, false);
                if ((info.flags & MediaCodec.BUFFER_FLAG_END_OF_STREAM) != 0) break;
            }
        }
    }

    private static int round2(int v) {
        return Math.max(2, v & ~1);
    }

    /** ARGB_8888 -> NV12 (YUV420 semi-planar). */
    private static void argbToNv12(Bitmap bmp, byte[] out, int w, int h) {
        int[] argb = new int[w * h];
        bmp.getPixels(argb, 0, w, 0, 0, w, h);
        int yPos = 0;
        int uvPos = w * h;
        for (int j = 0; j < h; j++) {
            for (int i = 0; i < w; i++) {
                int px = argb[j * w + i];
                int r = (px >> 16) & 0xFF;
                int g = (px >> 8) & 0xFF;
                int b = px & 0xFF;
                int y = ((66 * r + 129 * g + 25 * b + 128) >> 8) + 16;
                out[yPos++] = (byte) Math.max(0, Math.min(255, y));
                if ((j & 1) == 0 && (i & 1) == 0) {
                    int u = ((-38 * r - 74 * g + 112 * b + 128) >> 8) + 128;
                    int v = ((112 * r - 94 * g - 18 * b + 128) >> 8) + 128;
                    // NV12: interleaved U,V
                    out[uvPos++] = (byte) Math.max(0, Math.min(255, u));
                    out[uvPos++] = (byte) Math.max(0, Math.min(255, v));
                }
            }
        }
    }

    /** Suggested output file: Movies/Remote/remote_YYYYMMDD_HHmmss.mp4 */
    public static File defaultFile(File moviesDir) {
        File dir = new File(moviesDir, "Remote");
        if (!dir.exists()) dir.mkdirs();
        java.text.SimpleDateFormat f = new java.text.SimpleDateFormat(
                "yyyyMMdd_HHmmss", java.util.Locale.US);
        return new File(dir, "remote_" + f.format(new java.util.Date()) + ".mp4");
    }
}
