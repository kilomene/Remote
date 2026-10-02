package com.remote.viewer.features.terminal;

import android.content.Context;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.text.InputType;
import android.util.AttributeSet;
import android.view.KeyEvent;
import android.view.View;
import android.view.inputmethod.BaseInputConnection;
import android.view.inputmethod.EditorInfo;
import android.view.inputmethod.InputConnection;

import java.util.ArrayList;
import java.util.List;

/**
 * Minimal terminal emulator view: fixed grid, scrollback, ANSI SGR colors,
 * cursor addressing, clear screen / clear line. No external deps.
 *
 * Input: soft keyboard via onCreateInputConnection (raw bytes to the
 * listener), plus sendSpecial() for Esc/Tab/arrows/Ctrl combos.
 */
public class TerminalView extends View {

    public interface InputSink {
        void onBytes(byte[] data);
    }

    private static class Cell {
        char ch = ' ';
        int fg = 7; // 0-7 normal, 8-15 bright
        boolean bold;
    }

    private static class Line {
        final Cell[] cells;
        Line(int cols) {
            cells = new Cell[cols];
            for (int i = 0; i < cols; i++) cells[i] = new Cell();
        }
    }

    private static final int[] PALETTE = {
            0xFF000000, 0xFFAA0000, 0xFF00AA00, 0xFFAA5500,
            0xFF0000AA, 0xFFAA00AA, 0xFF00AAAA, 0xFFAAAAAA,
            0xFF555555, 0xFFFF5555, 0xFF55FF55, 0xFFFFFF55,
            0xFF5555FF, 0xFFFF55FF, 0xFF55FFFF, 0xFFFFFFFF,
    };

    private int cols = 80;
    private int rows = 24;
    private final List<Line> lines = new ArrayList<Line>();
    private int cursorRow;
    private int cursorCol;
    private int curFg = 7;
    private boolean curBold;
    private int scrollback = 500;

    private final Paint paint = new Paint();
    private float charW;
    private float charH;
    private float ascent;
    private int scrollOffset; // lines scrolled up from bottom
    private InputSink sink;

    public TerminalView(Context ctx) { super(ctx); init(); }
    public TerminalView(Context ctx, AttributeSet a) { super(ctx, a); init(); }

    private void init() {
        setBackgroundColor(Color.BLACK);
        setFocusable(true);
        setFocusableInTouchMode(true);
        paint.setTypeface(android.graphics.Typeface.MONOSPACE);
        paint.setAntiAlias(true);
        reset();
    }

    public void setInputSink(InputSink s) { sink = s; }

    public void setFontSizeSp(float sp) {
        paint.setTextSize(sp * getResources().getDisplayMetrics().scaledDensity);
        requestLayout();
        invalidate();
    }

    public synchronized void reset() {
        lines.clear();
        for (int i = 0; i < rows; i++) lines.add(new Line(cols));
        cursorRow = 0;
        cursorCol = 0;
        curFg = 7;
        curBold = false;
        scrollOffset = 0;
        postInvalidate();
    }

    public int getCols() { return cols; }
    public int getRows() { return rows; }

    /** Grid size listener: fired when the fitted grid changes (e.g. rotation). */
    public interface OnGridSizeListener {
        void onGridSize(int cols, int rows);
    }

    private OnGridSizeListener gridListener;

    public void setOnGridSizeListener(OnGridSizeListener l) { gridListener = l; }

    /**
     * Computes the grid that fits the current view size at the current font.
     * Returns {cols, rows}, clamped to sane minimums. Call after layout.
     */
    public int[] fitGrid() {
        float w = getWidth();
        float h = getHeight();
        float cw = charW > 0 ? charW : paint.measureText("M");
        Paint.FontMetrics fm = paint.getFontMetrics();
        float ch = fm.descent - fm.top;
        if (ch <= 0) ch = 20;
        int c = w > 0 && cw > 0 ? (int) (w / cw) : 80;
        int r = h > 0 && ch > 0 ? (int) (h / ch) : 24;
        return new int[]{Math.max(20, c), Math.max(8, r)};
    }

    /**
     * Resizes the grid, preserving existing content where it fits.
     * Cursor is clamped into the new grid.
     */
    public synchronized void resizeGrid(int newCols, int newRows) {
        newCols = Math.max(20, newCols);
        newRows = Math.max(8, newRows);
        if (newCols == cols && newRows == rows) return;
        List<Line> oldLines = new ArrayList<Line>(lines);
        int oldCols = cols;
        cols = newCols;
        rows = newRows;
        lines.clear();
        for (int i = 0; i < rows; i++) {
            Line ln = new Line(cols);
            if (i < oldLines.size()) {
                Line old = oldLines.get(i);
                int n = Math.min(oldCols, cols);
                for (int j = 0; j < n; j++) {
                    ln.cells[j].ch = old.cells[j].ch;
                    ln.cells[j].fg = old.cells[j].fg;
                    ln.cells[j].bold = old.cells[j].bold;
                }
            }
            lines.add(ln);
        }
        cursorRow = Math.max(0, Math.min(rows - 1, cursorRow));
        cursorCol = Math.max(0, Math.min(cols - 1, cursorCol));
        scrollOffset = 0;
        postInvalidate();
    }

    @Override
    protected void onSizeChanged(int w, int h, int oldw, int oldh) {
        super.onSizeChanged(w, h, oldw, oldh);
        if (w == oldw && h == oldh) return;
        int[] fit = fitGrid();
        if (fit[0] != cols || fit[1] != rows) {
            resizeGrid(fit[0], fit[1]);
            OnGridSizeListener l = gridListener;
            if (l != null) l.onGridSize(cols, rows);
        }
    }

    /** Appends raw pty output; parses a useful ANSI subset. */
    public synchronized void append(byte[] data) {
        if (data == null) return;
        int i = 0;
        while (i < data.length) {
            int b = data[i++] & 0xFF;
            if (b == 0x1B) {
                i = parseEscape(data, i);
            } else if (b == '\r') {
                cursorCol = 0;
            } else if (b == '\n') {
                newline();
            } else if (b == '\b' || b == 0x7F) {
                if (cursorCol > 0) cursorCol--;
            } else if (b == '\t') {
                cursorCol = Math.min(cols - 1, ((cursorCol / 8) + 1) * 8);
            } else if (b == 0x07) {
                // bell: ignore
            } else if (b >= 0x20) {
                putChar((char) b);
            }
        }
        postInvalidate();
    }

    private int parseEscape(byte[] data, int i) {
        if (i >= data.length) return i;
        int b = data[i++] & 0xFF;
        if (b == '[') {
            // CSI: collect params until final byte
            StringBuilder params = new StringBuilder();
            while (i < data.length) {
                int c = data[i++] & 0xFF;
                if (c >= 0x40 && c <= 0x7E) {
                    handleCsi(params.toString(), (char) c);
                    break;
                }
                params.append((char) c);
            }
        } else if (b == '(' || b == ')' || b == '#') {
            if (i < data.length) i++; // charset selection: skip one
        }
        // other escapes ignored
        return i;
    }

    private int[] csiParams(String s, int def) {
        if (s.isEmpty()) return new int[]{def};
        String[] parts = s.split(";");
        int[] out = new int[parts.length];
        for (int k = 0; k < parts.length; k++) {
            try {
                out[k] = parts[k].isEmpty() ? def : Integer.parseInt(parts[k]);
            } catch (NumberFormatException e) {
                out[k] = def;
            }
        }
        return out;
    }

    private void handleCsi(String params, char cmd) {
        switch (cmd) {
            case 'm': { // SGR
                int[] p = csiParams(params, 0);
                for (int n : p) {
                    if (n == 0) { curFg = 7; curBold = false; }
                    else if (n == 1) curBold = true;
                    else if (n >= 30 && n <= 37) curFg = n - 30;
                    else if (n >= 90 && n <= 97) curFg = n - 90 + 8;
                    else if (n == 39) curFg = 7;
                }
                break;
            }
            case 'H':
            case 'f': { // cursor position
                int[] p = csiParams(params, 1);
                int r = p.length > 0 ? p[0] : 1;
                int c = p.length > 1 ? p[1] : 1;
                int base = lines.size() - rows;
                cursorRow = clamp(base + r - 1, base, lines.size() - 1);
                cursorCol = clamp(c - 1, 0, cols - 1);
                break;
            }
            case 'A': cursorRow = clamp(cursorRow - csiParams(params, 1)[0], 0, lines.size() - 1); break;
            case 'B': cursorRow = clamp(cursorRow + csiParams(params, 1)[0], 0, lines.size() - 1); break;
            case 'C': cursorCol = clamp(cursorCol + csiParams(params, 1)[0], 0, cols - 1); break;
            case 'D': cursorCol = clamp(cursorCol - csiParams(params, 1)[0], 0, cols - 1); break;
            case 'J': { // ED
                int n = csiParams(params, 0)[0];
                if (n == 2) { // clear whole screen
                    lines.clear();
                    for (int k = 0; k < rows; k++) lines.add(new Line(cols));
                    cursorRow = lines.size() - rows;
                    cursorCol = 0;
                    scrollOffset = 0;
                } else { // clear from cursor down
                    for (int r = cursorRow; r < lines.size(); r++) {
                        Line ln = lines.get(r);
                        int start = (r == cursorRow) ? cursorCol : 0;
                        for (int c = start; c < cols; c++) { ln.cells[c].ch = ' '; ln.cells[c].fg = 7; }
                    }
                }
                break;
            }
            case 'K': { // EL: clear line
                Line ln = lines.get(clamp(cursorRow, 0, lines.size() - 1));
                for (int c = cursorCol; c < cols; c++) { ln.cells[c].ch = ' '; ln.cells[c].fg = 7; }
                break;
            }
            default: break;
        }
    }

    private static int clamp(int v, int lo, int hi) {
        return Math.max(lo, Math.min(hi, v));
    }

    private void putChar(char ch) {
        ensureCursorLine();
        Line ln = lines.get(cursorRow);
        if (cursorCol < cols) {
            Cell cell = ln.cells[cursorCol];
            cell.ch = ch;
            cell.fg = curFg;
            cell.bold = curBold;
            cursorCol++;
            if (cursorCol >= cols) {
                cursorCol = 0;
                newline();
            }
        }
    }

    private void ensureCursorLine() {
        while (cursorRow >= lines.size()) lines.add(new Line(cols));
    }

    private void newline() {
        cursorRow++;
        cursorCol = 0;
        ensureCursorLine();
        int max = rows + scrollback;
        while (lines.size() > max) {
            lines.remove(0);
            cursorRow--;
        }
        scrollOffset = 0;
    }

    // ---- input ---------------------------------------------------------------

    /** Sends raw escape sequences for special keys. */
    public void sendSpecial(String name) {
        InputSink s = sink;
        if (s == null) return;
        byte[] b = null;
        if ("esc".equals(name)) b = new byte[]{0x1B};
        else if ("tab".equals(name)) b = new byte[]{'\t'};
        else if ("up".equals(name)) b = new byte[]{0x1B, '[', 'A'};
        else if ("down".equals(name)) b = new byte[]{0x1B, '[', 'B'};
        else if ("right".equals(name)) b = new byte[]{0x1B, '[', 'C'};
        else if ("left".equals(name)) b = new byte[]{0x1B, '[', 'D'};
        else if ("home".equals(name)) b = new byte[]{0x1B, '[', 'H'};
        else if ("end".equals(name)) b = new byte[]{0x1B, '[', 'F'};
        else if ("pgup".equals(name)) b = new byte[]{0x1B, '[', '5', '~'};
        else if ("pgdn".equals(name)) b = new byte[]{0x1B, '[', '6', '~'};
        else if ("del".equals(name)) b = new byte[]{0x1B, '[', '3', '~'};
        else if ("ctrl-c".equals(name)) b = new byte[]{0x03};
        else if ("ctrl-d".equals(name)) b = new byte[]{0x04};
        else if ("ctrl-z".equals(name)) b = new byte[]{0x1A};
        else if ("ctrl-l".equals(name)) b = new byte[]{0x0C};
        if (b != null) s.onBytes(b);
    }

    @Override
    public InputConnection onCreateInputConnection(EditorInfo outAttrs) {
        outAttrs.inputType = InputType.TYPE_CLASS_TEXT
                | InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD;
        outAttrs.imeOptions = EditorInfo.IME_FLAG_NO_FULLSCREEN
                | EditorInfo.IME_FLAG_NO_EXTRACT_UI;
        return new BaseInputConnection(this, false) {
            @Override
            public boolean commitText(CharSequence text, int newCursorPosition) {
                InputSink s = sink;
                if (s != null && text != null) {
                    try {
                        s.onBytes(text.toString().getBytes("UTF-8"));
                    } catch (Exception ignored) {
                    }
                }
                return true;
            }

            @Override
            public boolean deleteSurroundingText(int beforeLength, int afterLength) {
                InputSink s = sink;
                if (s != null) s.onBytes(new byte[]{0x7F});
                return true;
            }

            @Override
            public boolean sendKeyEvent(KeyEvent event) {
                if (event.getAction() == KeyEvent.ACTION_DOWN) {
                    int kc = event.getKeyCode();
                    if (kc == KeyEvent.KEYCODE_DEL) {
                        InputSink s = sink;
                        if (s != null) s.onBytes(new byte[]{0x7F});
                        return true;
                    }
                    if (kc == KeyEvent.KEYCODE_ENTER) {
                        InputSink s = sink;
                        if (s != null) s.onBytes(new byte[]{'\r'});
                        return true;
                    }
                }
                return super.sendKeyEvent(event);
            }
        };
    }

    // ---- rendering -------------------------------------------------------------

    @Override
    protected void onMeasure(int wSpec, int hSpec) {
        super.onMeasure(wSpec, hSpec);
        Paint.FontMetrics fm = paint.getFontMetrics();
        charH = fm.descent - fm.top;
        ascent = -fm.top;
        charW = paint.measureText("M");
    }

    @Override
    protected void onDraw(Canvas canvas) {
        super.onDraw(canvas);
        if (charW <= 0) {
            Paint.FontMetrics fm = paint.getFontMetrics();
            charH = fm.descent - fm.top;
            ascent = -fm.top;
            charW = paint.measureText("M");
        }
        int lastVisible = lines.size() - 1 - scrollOffset;
        int firstVisible = Math.max(0, lastVisible - rows + 1);
        float y = ascent;
        StringBuilder sb = new StringBuilder();
        for (int r = firstVisible; r <= lastVisible; r++) {
            Line ln = lines.get(r);
            float x = 0;
            int runFg = -1;
            boolean runBold = false;
            sb.setLength(0);
            for (int c = 0; c < cols; c++) {
                Cell cell = ln.cells[c];
                if (cell.fg != runFg || cell.bold != runBold) {
                    flushRun(canvas, sb, x, y, runFg, runBold);
                    x += charW * sb.length();
                    sb.setLength(0);
                    runFg = cell.fg;
                    runBold = cell.bold;
                }
                sb.append(cell.ch);
            }
            flushRun(canvas, sb, x, y, runFg, runBold);
            // cursor
            if (r == cursorRow && scrollOffset == 0) {
                paint.setColor(0xFF4F7CFF);
                paint.setStyle(Paint.Style.FILL);
                canvas.drawRect(cursorCol * charW, y - ascent,
                        (cursorCol + 1) * charW, y - ascent + charH, paint);
            }
            y += charH;
        }
    }

    private void flushRun(Canvas canvas, StringBuilder sb, float x, float y,
                          int fg, boolean bold) {
        if (sb.length() == 0) return;
        int color = fg >= 0 && fg < PALETTE.length ? PALETTE[fg] : PALETTE[7];
        paint.setColor(color);
        paint.setFakeBoldText(bold);
        paint.setStyle(Paint.Style.FILL);
        canvas.drawText(sb, 0, sb.length(), x, y, paint);
    }

    /** Scrolls the view up/down through scrollback (positive = up). */
    public void scrollLines(int delta) {
        int max = Math.max(0, lines.size() - rows);
        scrollOffset = clamp(scrollOffset + delta, 0, max);
        invalidate();
    }

    public void scrollToBottom() {
        scrollOffset = 0;
        invalidate();
    }
}
