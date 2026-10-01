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
 * queued). Touch mapping: tap = left click, drag = mouse move (throttled),
 * long-press = right click, two-finger tap = right click, two-finger
 * drag/pinch = scroll. Coordinates are mapped through the fit-transform to
 * host pixels.
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

    private static final long MOVE_THROTTLE_MS = 33;
    private static final float TWO_FINGER_SCROLL_PX = 56f;

    private volatile Bitmap bitmap;
    private final Paint paint = new Paint();
    private final Rect dst = new Rect();
    private InputListener listener;

    // fit-transform of the last drawn frame (for touch -> host coords)
    private float scale = 1f;
    private int offX, offY, hostW, hostH;

    private final ExecutorService decode = Executors.newSingleThreadExecutor();
    private final Handler ui = new Handler(Looper.getMainLooper());
    private volatile long lastDecodeAt;

    private boolean haptics = true;
    private int fpsCap = 60;

    private final GestureDetector gestures;
    private long lastMoveSent;
    private float singleX, singleY;

    // two-finger state
    private boolean twoFinger;
    private boolean twoMoved;
    private boolean skipUntilUp;
    private long twoDownT;
    private float lastTwoY;
    private float twoAcc;

    public StreamView(Context ctx) { super(ctx); gestures = makeGestures(ctx); init(); }
    public StreamView(Context ctx, AttributeSet a) { super(ctx, a); gestures = makeGestures(ctx); init(); }

    private void init() {
        setBackgroundColor(Color.BLACK);
        setFocusable(true);
        setFocusableInTouchMode(true);
        paint.setFilterBitmap(true);
    }

    private GestureDetector makeGestures(Context ctx) {
        return new GestureDetector(ctx, new GestureDetector.SimpleOnGestureListener() {
            @Override
            public boolean onSingleTapConfirmed(MotionEvent e) {
                if (listener != null && hostW > 0) {
                    buzz();
                    int[] p = toHost(e.getX(), e.getY());
                    listener.onTap(p[0], p[1]);
                    return true;
                }
                return false;
            }

            @Override
            public void onLongPress(MotionEvent e) {
                if (listener != null && hostW > 0) {
                    buzz();
                    int[] p = toHost(e.getX(), e.getY());
                    listener.onRightClick(p[0], p[1]);
                }
            }
        });
    }

    public void setInputListener(InputListener l) { listener = l; }

    public void applyPrefs(Prefs p) {
        haptics = p.haptics;
        fpsCap = Math.max(1, p.fpsCap);
        paint.setFilterBitmap(p.smoothScaling);
    }

    /** Submits a raw JPEG frame; decoded on a background thread, fps-capped. */
    public void submitFrame(final byte[] jpeg) {
        if (jpeg == null || jpeg.length == 0) return;
        long now = System.currentTimeMillis();
        long minGap = 1000L / Math.max(1, fpsCap);
        if (now - lastDecodeAt < minGap) return; // drop, don't queue
        lastDecodeAt = now;
        decode.execute(new Runnable() {
            @Override public void run() {
                final Bitmap bmp = BitmapFactory.decodeByteArray(jpeg, 0, jpeg.length);
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
        invalidate();
    }

    @Override
    protected void onDraw(Canvas canvas) {
        super.onDraw(canvas);
        Bitmap bmp = bitmap;
        if (bmp == null) return;
        hostW = bmp.getWidth();
        hostH = bmp.getHeight();
        if (hostW <= 0 || hostH <= 0) return;
        scale = Math.min((float) getWidth() / hostW, (float) getHeight() / hostH);
        int dw = (int) (hostW * scale), dh = (int) (hostH * scale);
        offX = (getWidth() - dw) / 2;
        offY = (getHeight() - dh) / 2;
        dst.set(offX, offY, offX + dw, offY + dh);
        canvas.drawBitmap(bmp, null, dst, paint);
    }

    private int[] toHost(float x, float y) {
        int hx = hostW <= 1 ? 0 : (int) ((x - offX) / scale);
        int hy = hostH <= 1 ? 0 : (int) ((y - offY) / scale);
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

    @Override
    public boolean onTouchEvent(MotionEvent e) {
        if (listener == null) return true;
        int action = e.getActionMasked();
        long now = System.currentTimeMillis();

        // ---- two-finger gestures: tap = right click, drag/pinch = scroll ----
        if (action == MotionEvent.ACTION_POINTER_DOWN && e.getPointerCount() == 2 && !twoFinger) {
            twoFinger = true;
            twoMoved = false;
            twoDownT = now;
            twoAcc = 0;
            lastTwoY = (e.getY(0) + e.getY(1)) / 2f;
            return true;
        }
        if (twoFinger) {
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
                if (action != MotionEvent.ACTION_CANCEL && !twoMoved
                        && now - twoDownT < 450 && hostW > 0) {
                    buzz();
                    int idx = action == MotionEvent.ACTION_POINTER_UP
                            ? e.getActionIndex() : 0;
                    int[] p = toHost(e.getX(idx), e.getY(idx));
                    listener.onRightClick(p[0], p[1]);
                }
                twoFinger = false;
                skipUntilUp = true; // ignore the leftover single finger
                return true;
            }
            return true;
        }

        // ---- single finger ----
        if (action == MotionEvent.ACTION_DOWN) {
            skipUntilUp = false;
            requestFocus();
            singleX = e.getX();
            singleY = e.getY();
            if (hostW > 0) {
                int[] p = toHost(singleX, singleY);
                listener.onMove(p[0], p[1]);
            }
        } else if (action == MotionEvent.ACTION_MOVE) {
            if (!skipUntilUp && hostW > 0 && now - lastMoveSent > MOVE_THROTTLE_MS) {
                lastMoveSent = now;
                int[] p = toHost(e.getX(), e.getY());
                listener.onMove(p[0], p[1]);
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
