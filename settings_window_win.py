import json
import os
import sys
import tkinter as tk
from tkinter import ttk, messagebox
import sounddevice as sd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")

DEFAULT_SETTINGS = {
    "whisper_model": "turbo",
    "language": "auto",
    "microphone": "default",
    "hotkey_mod": "ctrl",
    "hotkey_key": "space",
    "hotkey_display": "Ctrl+Space",
    "trailing_space": True,
    "paste_mode": "clipboard_paste",
    "push_to_talk": False,
    "overlay_position": "center",
    "app_language": "es",
    "start_hidden": False,
    "launch_at_login": False,
    "memory_timeout": 0,
    "history_limit": 5,
    "history_retention": "keep_last"
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
    mics = [("default", "Default")]
    try:
        devices = sd.query_devices()
        for i, d in enumerate(devices):
            if d.get("max_input_channels", 0) > 0:
                mics.append((str(i), d["name"]))
    except Exception:
        pass
    return mics

class SettingsWindowWin:
    """Windows specific Settings UI using Tkinter."""
    def __init__(self, root, on_setting_changed=None, on_reload_dict=None):
        self.root = root
        self._on_setting_changed = on_setting_changed
        self._on_reload_dict = on_reload_dict
        self._config = load_settings()
        self.win = None
        self._capturing_hotkey = False
        
        # UI Variables
        self.vars = {}

    def show(self):
        if self.win is not None and self.win.winfo_exists():
            self.win.lift()
            self.win.focus_force()
            return

        self._config = load_settings()

        self.win = tk.Toplevel(self.root)
        self.win.title("QStrauss Voice - Configuración")
        self.win.geometry("500x650")
        self.win.resizable(False, False)
        self.win.configure(bg="#0b1133")
        
        # Use dark theme for ttk
        style = ttk.Style(self.win)
        style.theme_use('clam')
        style.configure("TLabel", background="#0b1133", foreground="#e8edf5", font=("Segoe UI", 10))
        style.configure("TCheckbutton", background="#0b1133", foreground="#e8edf5", font=("Segoe UI", 10))
        style.configure("TButton", font=("Segoe UI", 10))
        style.configure("Header.TLabel", foreground="#00c896", font=("Segoe UI", 10, "bold"))
        
        main_frame = tk.Frame(self.win, bg="#0b1133", padx=20, pady=10)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Header
        lbl_title = tk.Label(main_frame, text="QStrauss Voice", font=("Segoe UI", 16, "bold"), fg="#ffffff", bg="#0b1133")
        lbl_title.pack(pady=(10, 20))

        container = tk.Frame(main_frame, bg="#101a3a", padx=15, pady=10)
        container.pack(fill=tk.X, pady=5)

        # Helper to create rows
        def create_dropdown(parent, label_text, key, options, default):
            frame = tk.Frame(parent, bg="#101a3a")
            frame.pack(fill=tk.X, pady=5)
            tk.Label(frame, text=label_text, bg="#101a3a", fg="#e8edf5", font=("Segoe UI", 10)).pack(side=tk.LEFT)
            
            var = tk.StringVar(value=self._config.get(key, default))
            self.vars[key] = var
            
            combo = ttk.Combobox(frame, textvariable=var, state="readonly", width=25)
            combo['values'] = [v[1] for v in options]
            combo.pack(side=tk.RIGHT)
            
            # Map index back to value
            def on_change(event):
                idx = combo.current()
                if idx >= 0:
                    val = options[idx][0]
                    self._update_setting(key, val)
            
            # Select correct index
            for i, opt in enumerate(options):
                if opt[0] == var.get() or opt[0] == str(var.get()):
                    combo.current(i)
                    break
                    
            combo.bind("<<ComboboxSelected>>", on_change)
            return combo

        def create_toggle(parent, label_text, key, default):
            frame = tk.Frame(parent, bg="#101a3a")
            frame.pack(fill=tk.X, pady=5)
            tk.Label(frame, text=label_text, bg="#101a3a", fg="#e8edf5", font=("Segoe UI", 10)).pack(side=tk.LEFT)
            
            var = tk.BooleanVar(value=self._config.get(key, default))
            self.vars[key] = var
            
            def on_change():
                self._update_setting(key, var.get())
                
            chk = tk.Checkbutton(frame, variable=var, command=on_change, bg="#101a3a", activebackground="#101a3a", selectcolor="#101a3a")
            chk.pack(side=tk.RIGHT)

        ttk.Label(container, text="GENERAL", style="Header.TLabel").pack(anchor=tk.W, pady=(0, 5))

        # Hotkey logic
        hotkey_frame = tk.Frame(container, bg="#101a3a")
        hotkey_frame.pack(fill=tk.X, pady=5)
        tk.Label(hotkey_frame, text="Atajo de Transcripción", bg="#101a3a", fg="#e8edf5", font=("Segoe UI", 10)).pack(side=tk.LEFT)
        
        self.btn_hotkey = tk.Button(hotkey_frame, text=self._config.get("hotkey_display", "Ctrl+Space"), 
                                    bg="#162048", fg="white", relief="flat", command=self._start_capture)
        self.btn_hotkey.pack(side=tk.RIGHT)

        mics = _list_microphones()
        create_dropdown(container, "Idioma", "language", [("auto", "Auto-detect"), ("es", "Español"), ("en", "English")], "auto")
        create_dropdown(container, "Micrófono", "microphone", mics, "default")
        create_dropdown(container, "Modelo Whisper", "whisper_model", [("tiny", "tiny (rápido)"), ("base", "base"), ("small", "small (preciso)"), ("medium", "medium (muy preciso)"), ("turbo", "turbo (recomendado)"), ("large-v3", "large-v3 (máxima precisión)")], "turbo")

        # Output Tab
        container2 = tk.Frame(main_frame, bg="#101a3a", padx=15, pady=10)
        container2.pack(fill=tk.X, pady=10)
        ttk.Label(container2, text="SALIDA", style="Header.TLabel").pack(anchor=tk.W, pady=(0, 5))
        
        create_dropdown(container2, "Pegado Automático", "paste_mode", [("clipboard_paste", "Portapapeles + Pegado"), ("clipboard_only", "Solo portapapeles")], "clipboard_paste")
        create_toggle(container2, "Agregar Espacio Final", "trailing_space", True)

        # Footer Actions
        footer = tk.Frame(main_frame, bg="#0b1133")
        footer.pack(fill=tk.X, pady=10)
        
        tk.Button(footer, text="Recargar Diccionario", bg="#162048", fg="white", relief="flat", command=self._reload_dict_trigger).pack(side=tk.LEFT)
        tk.Button(footer, text="Cerrar", bg="#00c896", fg="black", font=("Segoe UI", 10, "bold"), relief="flat", command=self.win.destroy).pack(side=tk.RIGHT)

        self.win.bind("<Key>", self._on_key_press)

    def _update_setting(self, key, value):
        self._config[key] = value
        save_settings(self._config)
        if self._on_setting_changed:
            self._on_setting_changed(key, value)

    def _reload_dict_trigger(self):
        if self._on_reload_dict:
            self._on_reload_dict()
        messagebox.showinfo("Diccionario", "¡Diccionario recargado exitosamente!", parent=self.win)

    def _start_capture(self):
        self._capturing_hotkey = True
        self.btn_hotkey.config(text="Presiona teclas...", bg="#00c896", fg="black")
        self.win.focus_set()

    def _on_key_press(self, event):
        if not self._capturing_hotkey:
            return
            
        modifiers = []
        if event.state & 0x0004: modifiers.append("ctrl")
        if event.state & 0x0001: modifiers.append("shift")
        if event.state & 0x0008 or event.state & 0x20000: modifiers.append("alt")
        
        keysym = event.keysym.lower()
        if keysym in ('control_l', 'control_r', 'alt_l', 'alt_r', 'shift_l', 'shift_r', 'win_l'):
            return # Ignore pure modifiers until a key is pressed

        if keysym == 'space': keysym = 'space'
        elif keysym == 'return': keysym = 'return'
        elif keysym == 'escape': keysym = 'escape'

        mod = None
        if "ctrl" in modifiers: mod = "ctrl"
        elif "alt" in modifiers: mod = "alt"
        elif "shift" in modifiers: mod = "shift"
        else: mod = "ctrl"

        self._capturing_hotkey = False
        
        display = []
        if mod == "ctrl": display.append("Ctrl")
        if mod == "alt": display.append("Alt")
        if mod == "shift": display.append("Shift")
        
        raw_display = keysym.capitalize()
        display.append(raw_display)
        
        display_str = "+".join(display)
        self.btn_hotkey.config(text=display_str, bg="#162048", fg="white")
        
        self._update_setting("hotkey_mod", mod)
        self._update_setting("hotkey_key", keysym)
        self._update_setting("hotkey_display", display_str)

    def hide(self):
        if self.win is not None and self.win.winfo_exists():
            self.win.destroy()
            self.win = None

    def toggle(self):
        if self.win is not None and self.win.winfo_exists():
            self.hide()
        else:
            self.show()

    @property
    def config(self):
        return self._config
