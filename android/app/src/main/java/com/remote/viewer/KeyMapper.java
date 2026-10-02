package com.remote.viewer;

import android.view.KeyEvent;

/** Maps Android key codes to Remote wire-protocol v1 key names (see PROTOCOL.md). */
public final class KeyMapper {
    private KeyMapper() {}

    public static String map(int keyCode, KeyEvent event) {
        switch (keyCode) {
            case KeyEvent.KEYCODE_ENTER: return "Return";
            case KeyEvent.KEYCODE_NUMPAD_ENTER: return "Return";
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
            case KeyEvent.KEYCODE_META_LEFT: return "Super_L";
            case KeyEvent.KEYCODE_META_RIGHT: return "Super_R";
            case KeyEvent.KEYCODE_CAPS_LOCK: return "Caps_Lock";
            case KeyEvent.KEYCODE_FORWARD_DEL: return "Delete";
            case KeyEvent.KEYCODE_MOVE_HOME: return "Home";
            case KeyEvent.KEYCODE_MOVE_END: return "End";
            case KeyEvent.KEYCODE_PAGE_UP: return "Page_Up";
            case KeyEvent.KEYCODE_PAGE_DOWN: return "Page_Down";
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
}
