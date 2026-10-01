package com.remote.viewer;

import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.Rect;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
import android.util.AttributeSet;
import android.view.KeyEvent;
import android.view.MotionEvent;
import android.view.View;
import android.view.ViewConfiguration;
import android.view.inputmethod.BaseInputConnection;
import android.view.inputmethod.EditorInfo;
import android.view.inputmethod.InputConnection;

/**
 * Displays the host's screen (latest JPEG frame) and translates touch and
 * key events into Remote input events.
 *
 * Touch mapping: tap = left click, drag = mouse move, long-press = right click.
 */
public class StreamView extends View {

    public interface InputListener {
        void onMove(int x, int y);
        void onTap(int x, int y);       // left click
        void onRightClick(int x, int y);
        void onKey(String key, boolean down);
        void onText(char c);
    }

    private volatile Bitmap bitmap;
    private final Paint paint = new Paint();
    private final Rect dst = new Rect();
    private InputListener listener;

    // fit-transform of the last drawn frame (for touch -> host coords)
    private float scale = 1f;
    private int offX, offY, hostW, hostH;

    private final Handler handler = new Handler(Looper.getMainLooper());
    private float downX, downY;
    private long downTime;
    private boolean moved, longPressFired;
    private long lastMoveSent;

    public StreamView(Context ctx) { super(ctx); init(); }
    public StreamView(Context ctx, AttributeSet a) { super(ctx, a); init(); }

    private void init() {
        setBackgroundColor(Color.BLACK);
        setFocusable(true);
        setFocusableInTouchMode(true);
    }

    public void setInputListener(InputListener l) { listener = l; }

    public void setFrame(Bitmap bmp) {
        Bitmap old = bitmap;
        bitmap = bmp;
        if (old != null && old != bmp) old.recycle();
        postInvalidate();
    }

    @Override
    protected void onDraw(Canvas canvas) {
        super.onDraw(canvas);
        Bitmap bmp = bitmap;
        if (bmp == null) {
            paint.setColor(Color.GRAY);
            paint.setTextSize(48);
            canvas.drawText("connecting…", 60, 120, paint);
            return;
        }
        hostW = bmp.getWidth();
        hostH = bmp.getHeight();
        scale = Math.min((float) getWidth() / hostW, (float) getHeight() / hostH);
        int dw = (int) (hostW * scale), dh = (int) (hostH * scale);
        offX = (getWidth() - dw) / 2;
        offY = (getHeight() - dh) / 2;
        dst.set(offX, offY, offX + dw, offY + dh);
        canvas.drawBitmap(bmp, null, dst, paint);
    }

    private int[] toHost(float x, float y) {
        int hx = (int) ((x - offX) / scale);
        int hy = (int) ((y - offY) / scale);
        hx = Math.max(0, Math.min(hostW - 1, hx));
        hy = Math.max(0, Math.min(hostH - 1, hy));
        return new int[]{hx, hy};
    }

    private final Runnable longPress = new Runnable() {
        @Override public void run() {
            if (moved || listener == null) return;
            longPressFired = true;
            int[] p = toHost(downX, downY);
            listener.onRightClick(p[0], p[1]);
        }
    };

    @Override
    public boolean onTouchEvent(MotionEvent e) {
        if (listener == null || hostW == 0) return true;
        requestFocus();
        int action = e.getActionMasked();
        if (action == MotionEvent.ACTION_DOWN) {
            downX = e.getX(); downY = e.getY();
            downTime = System.currentTimeMillis();
            moved = false; longPressFired = false;
            int[] p = toHost(downX, downY);
            listener.onMove(p[0], p[1]);
            handler.postDelayed(longPress, ViewConfiguration.getLongPressTimeout());
        } else if (action == MotionEvent.ACTION_MOVE) {
            float dx = e.getX() - downX, dy = e.getY() - downY;
            if (dx * dx + dy * dy > 30 * 30) {
                moved = true;
                handler.removeCallbacks(longPress);
            }
            long now = System.currentTimeMillis();
            if (moved && now - lastMoveSent > 33) {
                lastMoveSent = now;
                int[] p = toHost(e.getX(), e.getY());
                listener.onMove(p[0], p[1]);
            }
        } else if (action == MotionEvent.ACTION_UP) {
            handler.removeCallbacks(longPress);
            int[] p = toHost(e.getX(), e.getY());
            if (!longPressFired && !moved
                    && System.currentTimeMillis() - downTime < 600) {
                listener.onTap(p[0], p[1]);
            }
        } else if (action == MotionEvent.ACTION_CANCEL) {
            handler.removeCallbacks(longPress);
        }
        return true;
    }

    // ---- keyboard: hardware keys ------------------------------------------------

    private static String mapKeyCode(int keyCode, KeyEvent event) {
        switch (keyCode) {
            case KeyEvent.KEYCODE_ENTER: return "Return";
            case KeyEvent.KEYCODE_DEL: return "BackSpace";
            case KeyEvent.KEYCODE_TAB: return "Tab";
            case KeyEvent.KEYCODE_ESCAPE: return "Escape";
            case KeyEvent.KEYCODE_DPAD_UP: return "Up";
            case KeyEvent.KEYCODE_DPAD_DOWN: return "Down";
            case KeyEvent.KEYCODE_DPAD_LEFT: return "Left";
            case KeyEvent.KEYCODE_DPAD_RIGHT: return "Right";
            case KeyEvent.KEYCODE_DPAD_CENTER: return "Return";
            case KeyEvent.KEYCODE_SPACE: return "space";
            case KeyEvent.KEYCODE_SHIFT_LEFT: return "Shift_L";
            case KeyEvent.KEYCODE_SHIFT_RIGHT: return "Shift_R";
            case KeyEvent.KEYCODE_CTRL_LEFT: return "Control_L";
            case KeyEvent.KEYCODE_CTRL_RIGHT: return "Control_R";
            case KeyEvent.KEYCODE_ALT_LEFT: return "Alt_L";
            case KeyEvent.KEYCODE_ALT_RIGHT: return "Alt_R";
            case KeyEvent.KEYCODE_F1: return "F1";
            case KeyEvent.KEYCODE_F2: return "F2";
            case KeyEvent.KEYCODE_F3: return "F3";
            case KeyEvent.KEYCODE_F4: return "F4";
            case KeyEvent.KEYCODE_F5: return "F5";
            case KeyEvent.KEYCODE_F6: return "F6";
            case KeyEvent.KEYCODE_F7: return "F7";
            case KeyEvent.KEYCODE_F8: return "F8";
            case KeyEvent.KEYCODE_F9: return "F9";
            case KeyEvent.KEYCODE_F10: return "F10";
            case KeyEvent.KEYCODE_F11: return "F11";
            case KeyEvent.KEYCODE_F12: return "F12";
            default: {
                int uni = event.getUnicodeChar();
                if (uni > 0 && uni < 0x10000) return String.valueOf((char) uni);
                return null;
            }
        }
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (listener != null) {
            String name = mapKeyCode(keyCode, event);
            if (name != null) { listener.onKey(name, true); return true; }
        }
        return super.onKeyDown(keyCode, event);
    }

    @Override
    public boolean onKeyUp(int keyCode, KeyEvent event) {
        if (listener != null) {
            String name = mapKeyCode(keyCode, event);
            if (name != null) { listener.onKey(name, false); return true; }
        }
        return super.onKeyUp(keyCode, event);
    }

    // ---- keyboard: soft (on-screen) input ---------------------------------------

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
                String name = mapKeyCode(event.getKeyCode(), event);
                if (name != null && listener != null) {
                    listener.onKey(name, event.getAction() == KeyEvent.ACTION_DOWN);
                    return true;
                }
                return super.sendKeyEvent(event);
            }
        };
    }
}
