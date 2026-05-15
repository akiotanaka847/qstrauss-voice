"""
settings_window.py — Native macOS settings window with WKWebView
Displays settings.html in a Cocoa NSWindow, bridges JS ↔ Python.
"""
import json
import os
import sys
import objc
import AppKit
import sounddevice as sd
from Foundation import NSMakeRect, NSObject, NSURL, NSURLRequest
from WebKit import WKWebView, WKWebViewConfiguration, WKUserContentController

IS_MAC = sys.platform == "darwin"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")
HTML_FILE = os.path.join(BASE_DIR, "resources", "settings.html")

DEFAULT_SETTINGS = {
    "whisper_model": "small",
    "language": "auto",
    "microphone": "default",
    "hotkey_mod": "alt" if IS_MAC else "ctrl",
    "hotkey_key": "space",
    "hotkey_display": "⌥ Space" if IS_MAC else "Ctrl+Space",
    "trailing_space": True,
    "paste_mode": "clipboard_paste",
    "push_to_talk": False,
    "overlay_position": "center",
    "app_language": "es",
}


def load_settings():
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        return {**DEFAULT_SETTINGS, **saved}
    return dict(DEFAULT_SETTINGS)


def save_settings(cfg):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def _list_microphones():
    mics = [{"id": "default", "name": "Default"}]
    try:
        devices = sd.query_devices()
        for i, d in enumerate(devices):
            if d["max_input_channels"] > 0:
                mics.append({"id": str(i), "name": d["name"]})
    except Exception:
        pass
    return mics


class _MessageHandler(NSObject):
    """Receives messages from JavaScript via window.webkit.messageHandlers."""

    def initWithCallback_(self, callback):
        self = objc.super(_MessageHandler, self).init()
        if self is not None:
            self._callback = callback
        return self

    def userContentController_didReceiveScriptMessage_(self, controller, message):
        try:
            data = json.loads(message.body())
            self._callback(data)
        except Exception as e:
            print(f"[settings] JS message error: {e}")


class SettingsWindow:
    """macOS window with embedded WKWebView showing settings.html."""

    def __init__(self, on_setting_changed=None, on_reload_dict=None):
        self._config = load_settings()
        self._on_setting_changed = on_setting_changed
        self._on_reload_dict = on_reload_dict
        self._build()

    def _build(self):
        W, H = 420, 640

        mask = (
            AppKit.NSWindowStyleMaskTitled
            | AppKit.NSWindowStyleMaskClosable
            | AppKit.NSWindowStyleMaskMiniaturizable
        )
        self._window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, W, H), mask, AppKit.NSBackingStoreBuffered, False
        )
        self._window.setTitle_("QStrauss Voice")
        self._window.center()

        # Dark appearance — navy brand color
        dark = AppKit.NSAppearance.appearanceNamed_("NSAppearanceNameDarkAqua")
        self._window.setAppearance_(dark)
        self._window.setTitlebarAppearsTransparent_(True)
        self._window.setBackgroundColor_(
            AppKit.NSColor.colorWithRed_green_blue_alpha_(0.043, 0.067, 0.20, 1.0)
        )

        # WebView
        config = WKWebViewConfiguration.alloc().init()
        controller = WKUserContentController.alloc().init()

        self._handler = _MessageHandler.alloc().initWithCallback_(self._on_js_message)
        controller.addScriptMessageHandler_name_(self._handler, "settings")
        config.setUserContentController_(controller)

        self._webview = WKWebView.alloc().initWithFrame_configuration_(
            NSMakeRect(0, 0, W, H), config
        )
        self._webview.setAutoresizingMask_(
            AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable
        )
        self._webview.setValue_forKey_(False, "drawsBackground")

        url = NSURL.fileURLWithPath_(HTML_FILE)
        self._webview.loadRequest_(NSURLRequest.requestWithURL_(url))

        self._window.contentView().addSubview_(self._webview)

        # Inject settings after page loads
        from Foundation import NSTimer
        self._handler._owner = self
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.5, self._handler, b"_injectSettings:", None, False
        )

    def _inject_settings(self):
        cfg = dict(self._config)
        cfg["microphones"] = _list_microphones()
        cfg["dict_count"] = self._count_dict_entries()
        try:
            from voice_typer import state
            cfg["model_ready"] = state["model"] is not None
        except Exception:
            cfg["model_ready"] = False
        model_labels = {
            "tiny": "Whisper Tiny (Local)",
            "base": "Whisper Base (Local)",
            "small": "Whisper Small (Local)",
            "medium": "Whisper Medium (Local)",
            "turbo": "Whisper Turbo (Recomendado)",
            "large-v3": "Whisper Large V3 (Máxima precisión)"
        }
        cfg["model_label"] = model_labels.get(
            cfg.get("whisper_model", "base"), "Whisper (Local)"
        )
        js = f"loadSettings({json.dumps(cfg)})"
        self._webview.evaluateJavaScript_completionHandler_(js, None)

    def _count_dict_entries(self):
        dict_file = os.path.join(BASE_DIR, "dictionary.json")
        try:
            with open(dict_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return len(data.get("corrections", {}))
        except Exception:
            return 0

    @objc.python_method
    def _on_js_message(self, data):
        action = data.get("action")

        if action == "set":
            key = data["key"]
            value = data["value"]
            self._config[key] = value
            save_settings(self._config)
            if self._on_setting_changed:
                self._on_setting_changed(key, value)

        elif action == "refresh_mics":
            mics = _list_microphones()
            js = f"""
                var sel = document.getElementById('microphone');
                sel.innerHTML = '';
                {json.dumps(mics)}.forEach(function(m) {{
                    var opt = document.createElement('option');
                    opt.value = m.id;
                    opt.textContent = m.name;
                    sel.appendChild(opt);
                }});
            """
            self._webview.evaluateJavaScript_completionHandler_(js, None)

        elif action == "reload_dict":
            if self._on_reload_dict:
                self._on_reload_dict()
            count = self._count_dict_entries()
            js = f"document.getElementById('dict-count').textContent = '{count} correcciones'"
            self._webview.evaluateJavaScript_completionHandler_(js, None)

    def show(self):
        # Temporarily become a regular app so window controls work fully
        AppKit.NSApp.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
        self._window.makeKeyAndOrderFront_(None)
        AppKit.NSApp.activateIgnoringOtherApps_(True)
        self._inject_settings()

    def hide(self):
        self._window.orderOut_(None)
        # Return to accessory (menu-bar-only) mode — no Dock icon
        AppKit.NSApp.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

    def toggle(self):
        if self._window.isVisible():
            self.hide()
        else:
            self.show()

    @property
    def config(self):
        return self._config


# Timer callback for ObjC NSTimer
def _inject_settings_selector(self, timer):
    if hasattr(self, "_owner"):
        self._owner._inject_settings()

_MessageHandler._injectSettings_ = _inject_settings_selector
