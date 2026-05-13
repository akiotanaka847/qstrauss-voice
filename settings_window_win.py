"""
settings_window_win.py — Windows settings window using pywebview (Edge WebView2)
Renders the same settings.html as the Mac WKWebView version.
"""
import json
import os
import sounddevice as sd

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")

DEFAULT_SETTINGS = {
    "whisper_model":    "turbo",
    "language":         "auto",
    "microphone":       "default",
    "hotkey_mod":       "ctrl",
    "hotkey_key":       "space",
    "hotkey_display":   "Ctrl + Space",
    "trailing_space":   True,
    "paste_mode":       "clipboard_paste",
    "push_to_talk":     False,
    "overlay_position": "center",
    "app_language":     "es",
    "start_hidden":     False,
    "launch_at_login":  False,
    "memory_timeout":   0,
    "history_limit":    5,
    "history_retention": "keep_last",
}

def load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            return {**DEFAULT_SETTINGS, **saved}
        except Exception:
            pass
    return dict(DEFAULT_SETTINGS)

def save_settings(cfg):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

def _list_microphones():
    mics = [{"id": "default", "name": "Default"}]
    try:
        for i, d in enumerate(sd.query_devices()):
            if d.get("max_input_channels", 0) > 0:
                mics.append({"id": str(i), "name": d["name"]})
    except Exception:
        pass
    return mics

def _count_dict():
    dict_file = os.path.join(BASE_DIR, "dictionary.json")
    try:
        with open(dict_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        return len(data.get("corrections", {}))
    except Exception:
        return 0


class SettingsApi:
    """JavaScript API exposed via window.pywebview.api in the settings window."""

    def __init__(self, on_setting_changed=None, on_reload_dict=None):
        self._config = load_settings()
        self._on_setting_changed = on_setting_changed
        self._on_reload_dict = on_reload_dict
        self._win = None  # set by run_windows() after create_window

    def on_message(self, json_str):
        data = json.loads(json_str)
        action = data.get("action")

        if action == "set":
            key, value = data["key"], data["value"]
            self._config[key] = value
            save_settings(self._config)
            if self._on_setting_changed:
                self._on_setting_changed(key, value)

        elif action == "refresh_mics":
            mics = _list_microphones()
            js = (
                "var s=document.getElementById('microphone');"
                "s.innerHTML='';"
                + json.dumps(mics)
                + ".forEach(function(m){"
                "var o=document.createElement('option');"
                "o.value=m.id;o.textContent=m.name;"
                "s.appendChild(o);});"
            )
            if self._win:
                self._win.evaluate_js(js)

        elif action == "reload_dict":
            if self._on_reload_dict:
                self._on_reload_dict()
            count = _count_dict()
            if self._win:
                self._win.evaluate_js(
                    f"document.getElementById('dict-count').textContent='{count} correcciones'"
                )

    def inject_settings(self, extra=None):
        cfg = dict(self._config)
        cfg["microphones"] = _list_microphones()
        cfg["dict_count"]  = _count_dict()
        model_labels = {
            "tiny":    "Whisper Tiny (Local)",
            "base":    "Whisper Base (Local)",
            "small":   "Whisper Small (Local)",
            "medium":  "Whisper Medium (Local)",
            "turbo":   "Whisper Turbo (Recomendado)",
            "large-v3": "Whisper Large V3 (Máxima precisión)",
        }
        cfg["model_label"] = model_labels.get(cfg.get("whisper_model", "base"), "Whisper (Local)")
        if extra:
            cfg.update(extra)
        js = f"if(typeof loadSettings==='function')loadSettings({json.dumps(cfg, ensure_ascii=False)})"
        if self._win:
            self._win.evaluate_js(js)

    @property
    def config(self):
        return self._config
