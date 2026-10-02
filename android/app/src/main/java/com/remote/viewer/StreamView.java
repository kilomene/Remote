package com.remote.viewer;

import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.Rect;
import android.os.Handler;
import android.os.Looper;
import android.os.VibrationEffect;
import android.os.Vibrator;
import android.text.InputType;
import android.util.AttributeSet;
import android.view.GestureDetector;
import android.view.KeyEvent;
import android.view.MotionEvent;
import android.view.View;
import android.view.inputmethod.BaseInputConnection;
import android.view.inputmethod.EditorInfo;
import android.view.inputmethod.InputConnection;

import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * Full-bleed MJPEG view for the host screen, plus touch/keyboard input.
 *
 * Frames are decoded on a background thread with an fps cap (dropped, not
 * queued).
 *
 * Scale modes: FIT (fit-to-screen) and ORIGINAL (1:1 pixels), plus pinch
 * zoom (1x-4x) and pan when zoomed. Double-tap resets the zoom.
 *
 * Control modes:
 * - TOUCH: tap = left click, drag = mouse move (throttled), long-press =
 *   right click, two-finger tap = right click, two-finger vertical drag =
 *   scroll. When zoomed, single-finger drag pans the viewport instead.
 * - TRACKPAD: single-finger drag moves a virtual cursor relatively
 *   (sensitivity-scaled, precision mode slows it); tap clicks at the cursor;
 *   two-finger vertical drag = wheel scroll.
 * - MOUSE: like trackpad with a visible pointer; on-screen L/R buttons
 *   (SessionActivity) click at the pointer; tap repositions the pointer.
 *
 * Coordinates are mapped through the fit/zoom/pan transform to host pixels.
 */
public class StreamView extends View {

    public interface InputListener {
        void onMove(int x, int y);
        void onTap(int x, int y);       // left click
        void onRightClick(int x, int y);
        void onScroll(int dx, int dy);
        void onKey(String key, boolean down);
        void onText(char c);
    }

    /** Called on the UI thread each time a new frame is swapped in. */
    public interface FrameListener {
        void onFrame(Bitmap bmp);
    }

    public static final int MODE_TOUCH = 0;
    public static final int MODE_TRACKPAD = 1;
    public static final int MODE_MOUSE = 2;

    public static final int SCALE_FIT = 0;
    public static final int SCALE_ORIGINAL = 1;

    private static final long MOVE_THROTTLE_MS = 33;
    private static final float TWO_FINGER_SCROLL_PX = 56f;
    private static final float PINCH_THRESHOLD_PX = 24f;
    private static final float MAX_ZOOM = 4f;

    private volatile Bitmap bitmap;
    private final Paint paint = new Paint();
    private final Paint cursorPaint = new Paint();
    private final Rect dst = new Rect();
    private InputListener listener;
    private FrameListener frameListener;

    // transform of the last drawn frame (for touch -> host coords)
    private float baseScale = 1f;   // fit or 1:1
    private float totalScale = 1f;  // baseScale * zoom
    private int offX, offY, hostW, hostH;

    private final ExecutorService decode = Executors.newSingleThreadExecutor();
    private final Handler ui = new Handler(Looper.getMainLooper());
    private volatile long lastDecodeAt;

    private boolean haptics = true;
    private int fpsCap = 60;
    private int decodeSample = 1;
    private int controlMode = MODE_TOUCH;
    private int scaleMode = SCALE_FIT;
    private boolean precision = false;
    private float sensitivity = 1f;

    // zoom / pan
    private float zoom = 1f;
    private float panX, panY;

    // virtual cursor (host pixels) for trackpad/mouse modes
    private final float[] cursor = new float[2];
    private boolean cursorInit;

    private final GestureDetector gestures;
    private long lastMoveSent;

    // two-finger state
    private boolean twoFinger;
    private boolean twoMoved;
    private boolean skipUntilUp;
    private long twoDownT;
    private float lastTwoY;
    private float twoAcc;
    private boolean pinchActive;
    private float pinchStartSpan;
    private float pinchStartZoom;

    // single-finger pan when zoomed
    private float panLastX, panLastY;

    public StreamView(Context ctx) { super(ctx); gestures = makeGestures(ctx); init(); }
    public StreamView(Context ctx, AttributeSet a) { super(ctx, a); gestures = makeGestures(ctx); init(); }

    private void init() {
        setBackgroundColor(Color.BLACK);
        setFocusable(true);
        setFocusableInTouchMode(true);
        paint.setFilterBitmap(true);
        cursorPaint.setColor(Color.WHITE);
        cursorPaint.setStyle(Paint.Style.STROKE);
        cursorPaint.setStrokeWidth(3f);
        cursorPaint.setAntiAlias(true);
    }

    private GestureDetector makeGestures(Context ctx) {
        return new GestureDetector(ctx, new GestureDetector.SimpleOnGestureListener() {
            @Override
            public boolean onSingleTapConfirmed(MotionEvent e) {
                if (listener == null || hostW <= 0) return false;
                buzz();
                if (controlMode == MODE_MOUSE) {
                    // tap repositions the pointer without clicking
                    int[] tp = toHost(e.getX(), e.getY());
                    moveCursorTo(tp[0], tp[1]);
                    listener.onMove(tp[0], tp[1]);
                    invalidate();
                    return true;
                }
                int[] p = controlMode == MODE_TOUCH
                        ? toHost(e.getX(), e.getY())
                        : cursorInt();
                if (controlMode == MODE_TOUCH) {
                    // keep the virtual cursor in sync so switching to
                    // mouse/trackpad mode (or the L/R buttons) doesn't
                    // jump to the screen center.
                    moveCursorTo(p[0], p[1]);
                }
                if (controlMode == MODE_TRACKPAD) listener.onMove(p[0], p[1]);
                listener.onTap(p[0], p[1]);
                return true;
            }

            @Override
            public void onLongPress(MotionEvent e) {
                if (listener == null || hostW <= 0) return;
                buzz();
                int[] p = controlMode == MODE_TOUCH
                        ? toHost(e.getX(), e.getY())
                        : cursorInt();
                if (controlMode == MODE_TOUCH) moveCursorTo(p[0], p[1]);
                if (controlMode != MODE_TOUCH) listener.onMove(p[0], p[1]);
                listener.onRightClick(p[0], p[1]);
            }

            @Override
            public boolean onDoubleTap(MotionEvent e) {
                resetZoom();
                return true;
            }
        });
    }

    public void setInputListener(InputListener l) { listener = l; }
    public void setFrameListener(FrameListener l) { frameListener = l; }

    public void applyPrefs(Prefs p) {
        haptics = p.haptics;
        fpsCap = Math.max(1, p.fpsCap);
        paint.setFilterBitmap(p.smoothScaling);
        String cm = Prefs.getString(getContext(), Prefs.K_CONTROL_MODE, "touch");
        controlMode = "trackpad".equals(cm) ? MODE_TRACKPAD
                : "mouse".equals(cm) ? MODE_MOUSE : MODE_TOUCH;
        scaleMode = "original".equals(
                Prefs.getString(getContext(), Prefs.K_SCALE_MODE, "fit"))
                ? SCALE_ORIGINAL : SCALE_FIT;
        precision = Prefs.getBool(getContext(), Prefs.K_PRECISION, false);
        sensitivity = Prefs.getInt(getContext(), Prefs.K_SENSITIVITY, 100) / 100f;
    }

    public void setControlMode(int mode) {
        controlMode = mode;
        Prefs.putString(getContext(), Prefs.K_CONTROL_MODE,
                mode == MODE_TRACKPAD ? "trackpad" : mode == MODE_MOUSE ? "mouse" : "touch");
        invalidate();
    }

    public int getControlMode() { return controlMode; }

    public void setScaleMode(int mode) {
        scaleMode = mode;
        Prefs.putString(getContext(), Prefs.K_SCALE_MODE,
                mode == SCALE_ORIGINAL ? "original" : "fit");
        clampPan();
        invalidate();
    }

    public int getScaleMode() { return scaleMode; }

    public void setPrecision(boolean on) {
        precision = on;
        Prefs.putBool(getContext(), Prefs.K_PRECISION, on);
    }

    public boolean getPrecision() { return precision; }

    /** Adaptive render scale: 1 = full, 2 = half resolution decode. */
    public void setDecodeSampleSize(int sample) {
        decodeSample = Math.max(1, sample);
    }

    public void resetZoom() {
        zoom = 1f;
        panX = 0;
        panY = 0;
        invalidate();
    }

    public float getZoom() { return zoom; }

    /** Left/right click at the virtual cursor (mouse mode buttons). */
    public void clickAtCursor(boolean right) {
        if (listener == null || hostW <= 0) return;
        ensureCursor();
        int[] p = cursorInt();
        buzz();
        if (right) listener.onRightClick(p[0], p[1]);
        else listener.onTap(p[0], p[1]);
    }

    private void ensureCursor() {
        if (!cursorInit && hostW > 0) {
            cursor[0] = hostW / 2f;
            cursor[1] = hostH / 2f;
            cursorInit = true;
        }
    }

    private int[] cursorInt() {
        ensureCursor();
        return new int[]{(int) cursor[0], (int) cursor[1]};
    }

    private void moveCursorTo(int x, int y) {
        cursor[0] = Math.max(0, Math.min(hostW - 1, x));
        cursor[1] = Math.max(0, Math.min(hostH - 1, y));
        cursorInit = true;
    }

    /** Submits a raw JPEG frame; decoded on a background thread, fps-capped. */
    public void submitFrame(final byte[] jpeg) {
        if (jpeg == null || jpeg.length == 0) return;
        long now = System.currentTimeMillis();
        long minGap = 1000L / Math.max(1, fpsCap);
        if (now - lastDecodeAt < minGap) return; // drop, don't queue
        lastDecodeAt = now;
        final int sample = decodeSample;
        decode.execute(new Runnable() {
            @Override public void run() {
                BitmapFactory.Options opts = new BitmapFactory.Options();
                opts.inSampleSize = sample;
                final Bitmap bmp = BitmapFactory.decodeByteArray(
                        jpeg, 0, jpeg.length, opts);
                if (bmp == null) return;
                ui.post(new Runnable() {
                    @Override public void run() { swapBitmap(bmp); }
                });
            }
        });
    }

    private void swapBitmap(Bitmap bmp) {
        Bitmap old = bitmap;
        bitmap = bmp;
        if (old != null && old != bmp) old.recycle();
        FrameListener fl = frameListener;
        if (fl != null) fl.onFrame(bmp);
        invalidate();
    }

    /** Returns a copy of the current frame, or null. Caller must recycle. */
    public synchronized Bitmap snapshotCopy() {
        Bitmap b = bitmap;
        if (b == null || b.isRecycled()) return null;
        try {
            return b.copy(b.getConfig() != null
                    ? b.getConfig() : Bitmap.Config.ARGB_8888, false);
        } catch (Exception e) {
            return null;
        }
    }

    @Override
    protected void onDraw(Canvas canvas) {
        super.onDraw(canvas);
        Bitmap bmp = bitmap;
        if (bmp == null) return;
        hostW = bmp.getWidth();
        hostH = bmp.getHeight();
        if (hostW <= 0 || hostH <= 0) return;
        if (scaleMode == SCALE_FIT) {
            baseScale = Math.min((float) getWidth() / hostW,
                    (float) getHeight() / hostH);
        } else {
            baseScale = 1f;
        }
        totalScale = baseScale * zoom;
        int dw = (int) (hostW * totalScale), dh = (int) (hostH * totalScale);
        clampPan();
        offX = (int) ((getWidth() - dw) / 2 + panX);
        offY = (int) ((getHeight() - dh) / 2 + panY);
        dst.set(offX, offY, offX + dw, offY + dh);
        canvas.drawBitmap(bmp, null, dst, paint);

        if ((controlMode == MODE_TRACKPAD || controlMode == MODE_MOUSE)
                && cursorInit) {
            float cx = offX + cursor[0] * totalScale;
            float cy = offY + cursor[1] * totalScale;
            float r = 14 * getResources().getDisplayMetrics().density / 3f;
            canvas.drawCircle(cx, cy, r, cursorPaint);
            canvas.drawLine(cx - r * 1.6f, cy, cx + r * 1.6f, cy, cursorPaint);
            canvas.drawLine(cx, cy - r * 1.6f, cx, cy + r * 1.6f, cursorPaint);
        }
    }

    private void clampPan() {
        if (hostW <= 0 || zoom <= 1.01f) {
            panX = 0;
            panY = 0;
            return;
        }
        int dw = (int) (hostW * baseScale * zoom);
        int dh = (int) (hostH * baseScale * zoom);
        float maxX = Math.max(0, (dw - getWidth()) / 2f);
        float maxY = Math.max(0, (dh - getHeight()) / 2f);
        panX = Math.max(-maxX, Math.min(maxX, panX));
        panY = Math.max(-maxY, Math.min(maxY, panY));
    }

    private int[] toHost(float x, float y) {
        int hx = totalScale <= 0 ? 0 : (int) ((x - offX) / totalScale);
        int hy = totalScale <= 0 ? 0 : (int) ((y - offY) / totalScale);
        hx = Math.max(0, Math.min(hostW - 1, hx));
        hy = Math.max(0, Math.min(hostH - 1, hy));
        return new int[]{hx, hy};
    }

    private void buzz() {
        if (!haptics) return;
        try {
            Vibrator v = (Vibrator) getContext().getSystemService(Context.VIBRATOR_SERVICE);
            if (v != null && v.hasVibrator()) {
                v.vibrate(VibrationEffect.createOneShot(18, VibrationEffect.DEFAULT_AMPLITUDE));
            }
        } catch (Exception ignored) {
        }
    }

    private static float span(MotionEvent e) {
        if (e.getPointerCount() < 2) return 0;
        float dx = e.getX(0) - e.getX(1);
        float dy = e.getY(0) - e.getY(1);
        return (float) Math.hypot(dx, dy);
    }

    @Override
    public boolean onTouchEvent(MotionEvent e) {
        if (listener == null) return true;
        int action = e.getActionMasked();
        long now = System.currentTimeMillis();

        // ---- pinch zoom (any mode): two pointers, span changing --------------
        if (action == MotionEvent.ACTION_POINTER_DOWN && e.getPointerCount() == 2) {
            twoFinger = true;
            twoMoved = false;
            twoDownT = now;
            twoAcc = 0;
            lastTwoY = (e.getY(0) + e.getY(1)) / 2f;
            pinchActive = false;
            pinchStartSpan = span(e);
            pinchStartZoom = zoom;
            return true;
        }
        if (twoFinger) {
            if (pinchActive || (action == MotionEvent.ACTION_MOVE
                    && e.getPointerCount() == 2)) {
                float s = span(e);
                if (!pinchActive && pinchStartSpan > 0
                        && Math.abs(s - pinchStartSpan) > PINCH_THRESHOLD_PX) {
                    pinchActive = true;
                    twoMoved = true;
                }
                if (pinchActive) {
                    if (pinchStartSpan > 0) {
                        zoom = Math.max(1f, Math.min(MAX_ZOOM,
                                pinchStartZoom * s / pinchStartSpan));
                        clampPan();
                        invalidate();
                    }
                    return true;
                }
            }
            // ---- two-finger (non-pinch): tap = right click, drag = scroll ----
            if (action == MotionEvent.ACTION_MOVE && e.getPointerCount() == 2) {
                float y = (e.getY(0) + e.getY(1)) / 2f;
                float dy = y - lastTwoY;
                lastTwoY = y;
                if (Math.abs(dy) > 2) twoMoved = true;
                twoAcc += dy;
                while (twoAcc > TWO_FINGER_SCROLL_PX) {
                    twoAcc -= TWO_FINGER_SCROLL_PX;
                    listener.onScroll(0, 1);
                }
                while (twoAcc < -TWO_FINGER_SCROLL_PX) {
                    twoAcc += TWO_FINGER_SCROLL_PX;
                    listener.onScroll(0, -1);
                }
                return true;
            }
            if (action == MotionEvent.ACTION_POINTER_UP || action == MotionEvent.ACTION_UP
                    || action == MotionEvent.ACTION_CANCEL) {
                if (!pinchActive && action != MotionEvent.ACTION_CANCEL && !twoMoved
                        && now - twoDownT < 450 && hostW > 0) {
                    buzz();
                    int[] p = controlMode == MODE_TOUCH
                            ? toHost(e.getX(e.getActionIndex()), e.getY(e.getActionIndex()))
                            : cursorInt();
                    if (controlMode == MODE_TOUCH) moveCursorTo(p[0], p[1]);
                    listener.onRightClick(p[0], p[1]);
                }
                twoFinger = false;
                pinchActive = false;
                skipUntilUp = true; // ignore the leftover single finger
                return true;
            }
            return true;
        }

        // ---- single finger ----
        boolean zoomed = zoom > 1.01f;
        if (action == MotionEvent.ACTION_DOWN) {
            skipUntilUp = false;
            requestFocus();
            // pan anchor doubles as the relative-delta anchor for
            // trackpad/mouse mode
            panLastX = e.getX();
            panLastY = e.getY();
            if (!zoomed && controlMode == MODE_TOUCH && hostW > 0) {
                int[] p = toHost(e.getX(), e.getY());
                moveCursorTo(p[0], p[1]);
                listener.onMove(p[0], p[1]);
            }
        } else if (action == MotionEvent.ACTION_MOVE) {
            if (skipUntilUp) return true;
            if (zoomed) {
                panX += e.getX() - panLastX;
                panY += e.getY() - panLastY;
                panLastX = e.getX();
                panLastY = e.getY();
                clampPan();
                invalidate();
            } else if (hostW > 0 && now - lastMoveSent > MOVE_THROTTLE_MS) {
                lastMoveSent = now;
                if (controlMode == MODE_TOUCH) {
                    int[] p = toHost(e.getX(), e.getY());
                    moveCursorTo(p[0], p[1]);
                    listener.onMove(p[0], p[1]);
                } else {
                    // trackpad / mouse: relative cursor movement
                    ensureCursor();
                    float mult = sensitivity * (precision ? 0.4f : 1f);
                    float dx = e.getX() - panLastX;
                    float dy = e.getY() - panLastY;
                    cursor[0] = Math.max(0, Math.min(hostW - 1,
                            cursor[0] + dx / totalScale * mult));
                    cursor[1] = Math.max(0, Math.min(hostH - 1,
                            cursor[1] + dy / totalScale * mult));
                    int[] p = cursorInt();
                    listener.onMove(p[0], p[1]);
                    invalidate();
                }
                panLastX = e.getX();
                panLastY = e.getY();
            }
        } else if (action == MotionEvent.ACTION_UP || action == MotionEvent.ACTION_CANCEL) {
            skipUntilUp = false;
        }
        gestures.onTouchEvent(e);
        return true;
    }

    // ---- keyboard: hardware keys --------------------------------------------

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (listener != null) {
            String name = KeyMapper.map(keyCode, event);
            if (name != null) { listener.onKey(name, true); return true; }
        }
        return super.onKeyDown(keyCode, event);
    }

    @Override
    public boolean onKeyUp(int keyCode, KeyEvent event) {
        if (listener != null) {
            String name = KeyMapper.map(keyCode, event);
            if (name != null) { listener.onKey(name, false); return true; }
        }
        return super.onKeyUp(keyCode, event);
    }

    // ---- keyboard: soft (on-screen) input ------------------------------------

    @Override
    public InputConnection onCreateInputConnection(EditorInfo outAttrs) {
        outAttrs.inputType = InputType.TYPE_CLASS_TEXT
                | InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD;
        outAttrs.imeOptions = EditorInfo.IME_FLAG_NO_FULLSCREEN
                | EditorInfo.IME_FLAG_NO_EXTRACT_UI;
        return new BaseInputConnection(this, false) {
            @Override
            public boolean commitText(CharSequence text, int newCursorPosition) {
                if (listener != null) {
                    for (int i = 0; i < text.length(); i++) listener.onText(text.charAt(i));
                }
                return true;
            }

            @Override
            public boolean deleteSurroundingText(int beforeLength, int afterLength) {
                if (listener != null) {
                    listener.onKey("BackSpace", true);
                    listener.onKey("BackSpace", false);
                }
                return true;
            }

            @Override
            public boolean sendKeyEvent(KeyEvent event) {
                String name = KeyMapper.map(event.getKeyCode(), event);
                if (name != null && listener != null) {
                    listener.onKey(name, event.getAction() == KeyEvent.ACTION_DOWN);
                    return true;
                }
                return super.sendKeyEvent(event);
            }
        };
    }

    public void shutdown() {
        decode.shutdownNow();
        Bitmap b = bitmap;
        bitmap = null;
        if (b != null) b.recycle();
    }
}
