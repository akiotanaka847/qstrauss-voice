"""
QStrauss Voice — Invisible voice-to-text app
Press your hotkey → popup appears → speak → press hotkey again → text pastes at cursor.
No menu bar icon. No dock icon. Completely hidden.
Audio never leaves your computer.
"""

import os
import re
import sys
import json
import threading
import tempfile
import time
import subprocess
import numpy as np
import sounddevice as sd
import pyperclip
import scipy.io.wavfile as wav
from faster_whisper import WhisperModel

IS_MAC     = sys.platform == "darwin"
IS_WINDOWS = sys.platform == "win32"

APP_NAME        = "QStrauss Voice"
BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
DICTIONARY_FILE = os.path.join(BASE_DIR, "dictionary.json")
RESOURCES_DIR   = os.path.join(BASE_DIR, "resources")
SETTINGS_FILE   = os.path.join(BASE_DIR, "settings.json")
SAMPLE_RATE     = 16000

# ─── Settings ────────────────────────────────────────────────────────────────

DEFAULT_SETTINGS = {
    "whisper_model": "base",
    "language": "auto",
    "microphone": "default",
    "hotkey_mod": "alt" if IS_MAC else "ctrl",
    "hotkey_key": "space",
    "hotkey_display": "⌥ Space" if IS_MAC else "Ctrl+Space",
    "trailing_space": True,
    "paste_mode": "clipboard_paste",
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

settings = load_settings()

# ─── Shared state ────────────────────────────────────────────────────────────

state = {
    "recording":      False,
    "audio_chunks":   [],
    "corrections":    {},
    "initial_prompt": "",
    "model":          None,
}
lock = threading.Lock()

# ─── Dictionary ──────────────────────────────────────────────────────────────

def load_dictionary():
    if not os.path.exists(DICTIONARY_FILE):
        return {}, ""
    with open(DICTIONARY_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    corrections    = {k.lower(): v for k, v in data.get("corrections", {}).items()}
    initial_prompt = ", ".join(data.get("hints", []))
    return corrections, initial_prompt

def reload_dictionary():
    c, p = load_dictionary()
    state["corrections"]    = c
    state["initial_prompt"] = p
    print(f"Dictionary: {len(c)} corrections loaded")

# ─── Sound effects ───────────────────────────────────────────────────────────

SFX_START = "/System/Library/Sounds/Pop.aiff"
SFX_STOP  = "/System/Library/Sounds/Tink.aiff"

def play_sfx(path):
    if IS_MAC and os.path.exists(path):
        subprocess.Popen(["afplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

# ─── Audio stream ────────────────────────────────────────────────────────────

audio_stream = None

_audio_cb_count = 0

def audio_callback(indata, frames, time_info, status):
    global _audio_cb_count
    _audio_cb_count += 1
    if status:
        log(f"[audio_cb] status: {status}")
    if state["recording"]:
        state["audio_chunks"].append(indata.copy())
        if len(state["audio_chunks"]) % 50 == 1:
            log(f"[audio_cb] chunk #{len(state['audio_chunks'])}, frames={frames}")

def start_audio_stream():
    global audio_stream
    mic = settings.get("microphone", "default")
    device = None if mic == "default" else int(mic)
    log(f"Opening audio stream: device={device}, rate={SAMPLE_RATE}")
    try:
        audio_stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            callback=audio_callback,
            device=device,
        )
        audio_stream.start()
        log(f"Audio stream ready: active={audio_stream.active}")
    except Exception as e:
        log(f"Audio stream error: {e}")
        # Try again with default device
        try:
            audio_stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="float32",
                callback=audio_callback,
            )
            audio_stream.start()
            log(f"Audio stream ready (default fallback): active={audio_stream.active}")
        except Exception as e2:
            log(f"Audio stream FATAL: {e2}")

# ─── Language ────────────────────────────────────────────────────────────────

def current_language():
    lang = settings.get("language", "auto")
    return None if lang == "auto" else lang

# ─── Recording ───────────────────────────────────────────────────────────────

def start_recording(update_ui=None):
    global _audio_cb_count
    log(f"start_recording called (audio_cb_count={_audio_cb_count}, stream_active={audio_stream.active if audio_stream else 'None'})")
    if state["model"] is None:
        log("Model not ready yet")
        return
    with lock:
        if state["recording"]:
            return
        state["recording"]    = True
        state["audio_chunks"] = []
    _audio_cb_count = 0
    play_sfx(SFX_START)
    log("Recording started — speak now")
    if update_ui:
        update_ui("recording")

def stop_and_transcribe(update_ui=None):
    with lock:
        if not state["recording"]:
            return
        state["recording"] = False
        chunks = list(state["audio_chunks"])
        state["audio_chunks"] = []

    log(f"stop_and_transcribe: {len(chunks)} chunks, audio_cb_count={_audio_cb_count}")
    play_sfx(SFX_STOP)

    if update_ui:
        update_ui("transcribing")

    if not chunks:
        log("No audio captured — chunks list was empty")
        if update_ui:
            update_ui("idle")
        return

    audio = np.concatenate(chunks, axis=0).flatten().astype(np.float32)
    duration = len(audio) / SAMPLE_RATE
    peak = float(np.max(np.abs(audio)))
    rms = float(np.sqrt(np.mean(audio ** 2)))
    log(f"Transcribing {duration:.1f}s of audio ({len(audio)} samples, peak={peak:.4f}, rms={rms:.6f})")

    if peak < 0.001:
        log("WARNING: Audio is essentially silence — microphone may not have permission")
        if update_ui:
            update_ui("idle")
        return

    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    wav.write(tmp.name, SAMPLE_RATE, audio)

    try:
        segs, info = state["model"].transcribe(
            tmp.name,
            language=current_language(),
            beam_size=5,
            vad_filter=True,
            initial_prompt=state["initial_prompt"] or None,
        )
        text = " ".join(s.text for s in segs).strip()
        log(f"Whisper result: '{text}' (lang={info.language}, prob={info.language_probability:.2f})")
    except Exception as e:
        log(f"Transcription ERROR: {e}")
        if update_ui:
            update_ui("idle")
        return
    finally:
        os.unlink(tmp.name)

    if update_ui:
        update_ui("idle")

    if not text:
        log("No speech detected")
        return

    for wrong, correct in state["corrections"].items():
        if wrong in text.lower():
            text = re.sub(re.escape(wrong), correct, text, flags=re.IGNORECASE)

    if settings.get("trailing_space", True):
        text += " "

    log(f"Transcribed: {text.strip()}")

    pyperclip.copy(text)
    time.sleep(0.08)

    if IS_MAC:
        try:
            subprocess.run(
                ["osascript", "-e",
                 'tell application "System Events" to keystroke "v" using command down'],
                timeout=3,
            )
            log("Paste command sent")
        except Exception as e:
            log(f"Paste error: {e}")
    else:
        from pynput import keyboard as kb
        ctrl = kb.Controller()
        with ctrl.pressed(kb.Key.ctrl):
            ctrl.press('v')
            ctrl.release('v')

def toggle_recording(update_ui=None):
    log(f"toggle_recording: recording={state['recording']}")
    if state["recording"]:
        threading.Thread(
            target=stop_and_transcribe, kwargs={"update_ui": update_ui}, daemon=True
        ).start()
    else:
        start_recording(update_ui=update_ui)

# ─── Hotkey listener ─────────────────────────────────────────────────────────

# Must keep references alive to prevent garbage collection
_hotkey_refs = []

def start_hotkey_listener(update_ui=None):
    if IS_MAC:
        _start_hotkey_mac(update_ui)
    elif IS_WINDOWS:
        _start_hotkey_windows(update_ui)

# ── macOS: Carbon API (no Accessibility permission needed) ──

_KEYCODE_MAP = {
    "space": 49, "return": 36, "tab": 48, "escape": 53,
    "a": 0, "b": 11, "c": 8, "d": 2, "e": 14, "f": 3, "g": 5,
    "h": 4, "i": 34, "j": 38, "k": 40, "l": 37, "m": 46, "n": 45,
    "o": 31, "p": 35, "q": 12, "r": 15, "s": 1, "t": 17, "u": 32,
    "v": 9, "w": 13, "x": 7, "y": 16, "z": 6,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97,
    "f7": 98, "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
}

_MODIFIER_MAP = {
    "alt": 0x0800, "option": 0x0800,
    "cmd": 0x0100, "command": 0x0100,
    "ctrl": 0x1000, "control": 0x1000,
    "shift": 0x0200,
}

def _start_hotkey_mac(update_ui=None):
    import ctypes
    from ctypes import c_void_p, c_uint32, c_int32, Structure, byref, CFUNCTYPE, POINTER

    class EventHotKeyID(Structure):
        _fields_ = [("signature", c_uint32), ("id", c_uint32)]

    class EventTypeSpec(Structure):
        _fields_ = [("eventClass", c_uint32), ("eventKind", c_uint32)]

    carbon = ctypes.cdll.LoadLibrary(
        "/System/Library/Frameworks/Carbon.framework/Carbon"
    )
    carbon.GetApplicationEventTarget.restype = c_void_p
    carbon.InstallEventHandler.argtypes = [
        c_void_p, ctypes.c_void_p, c_uint32, POINTER(EventTypeSpec), c_void_p, POINTER(c_void_p),
    ]
    carbon.InstallEventHandler.restype = c_int32
    carbon.RegisterEventHotKey.argtypes = [
        c_uint32, c_uint32, EventHotKeyID, c_void_p, c_uint32, POINTER(c_void_p),
    ]
    carbon.RegisterEventHotKey.restype = c_int32

    EventHandlerProc = CFUNCTYPE(c_int32, c_void_p, c_void_p, c_void_p)

    def _on_hotkey(next_handler, event, user_data):
        log(">>> HOTKEY PRESSED <<<")
        toggle_recording(update_ui=update_ui)
        return 0

    handler_func = EventHandlerProc(_on_hotkey)
    _hotkey_refs.append(handler_func)

    kEventClassKeyboard = 0x6B657962
    kEventHotKeyPressed = 5
    event_type = EventTypeSpec(kEventClassKeyboard, kEventHotKeyPressed)
    handler_ref = c_void_p()

    err = carbon.InstallEventHandler(
        carbon.GetApplicationEventTarget(), handler_func,
        c_uint32(1), byref(event_type), None, byref(handler_ref),
    )
    if err != 0:
        log(f"InstallEventHandler failed: {err}")
        return
    _hotkey_refs.append(handler_ref)

    mod_name = settings.get("hotkey_mod", "alt")
    key_name = settings.get("hotkey_key", "space")
    modifier = _MODIFIER_MAP.get(mod_name.lower(), 0x0800)
    keycode = _KEYCODE_MAP.get(key_name.lower(), 49)

    hotkey_id = EventHotKeyID(0x51565F31, 1)
    hotkey_ref = c_void_p()
    err = carbon.RegisterEventHotKey(
        c_uint32(keycode), c_uint32(modifier), hotkey_id,
        carbon.GetApplicationEventTarget(), c_uint32(0), byref(hotkey_ref),
    )
    if err != 0:
        log(f"RegisterEventHotKey failed: {err}")
        return
    _hotkey_refs.append(hotkey_ref)
    log(f"Carbon hotkey registered: {mod_name}+{key_name}")

# ── Windows: pynput global hotkey ──

def _start_hotkey_windows(update_ui=None):
    from pynput import keyboard

    mod_name = settings.get("hotkey_mod", "ctrl")
    key_name = settings.get("hotkey_key", "space")

    # Map modifier names to pynput keys
    _WIN_MOD_MAP = {
        "ctrl": keyboard.Key.ctrl_l, "control": keyboard.Key.ctrl_l,
        "alt": keyboard.Key.alt_l, "option": keyboard.Key.alt_l,
        "shift": keyboard.Key.shift_l,
        "cmd": keyboard.Key.cmd, "command": keyboard.Key.cmd,
    }

    # Map key names to pynput keys
    _WIN_KEY_MAP = {
        "space": keyboard.Key.space, "return": keyboard.Key.enter,
        "tab": keyboard.Key.tab, "escape": keyboard.Key.esc,
    }
    # Add F-keys
    for i in range(1, 13):
        _WIN_KEY_MAP[f"f{i}"] = getattr(keyboard.Key, f"f{i}")

    mod_key = _WIN_MOD_MAP.get(mod_name.lower(), keyboard.Key.ctrl_l)
    main_key = _WIN_KEY_MAP.get(key_name.lower())
    if main_key is None:
        # Single character key
        try:
            main_key = keyboard.KeyCode.from_char(key_name.lower())
        except Exception:
            main_key = keyboard.Key.space

    pressed_keys = set()

    def on_press(key):
        pressed_keys.add(key)
        if mod_key in pressed_keys and (key == main_key or key == main_key):
            log(">>> HOTKEY PRESSED <<<")
            toggle_recording(update_ui=update_ui)

    def on_release(key):
        pressed_keys.discard(key)

    listener = keyboard.Listener(on_press=on_press, on_release=on_release)
    listener.daemon = True
    listener.start()
    _hotkey_refs.append(listener)
    log(f"pynput hotkey registered: {mod_name}+{key_name}")

# ─── Load model ──────────────────────────────────────────────────────────────

def load_model():
    model_name = settings.get("whisper_model", "base")
    log(f"Loading Whisper '{model_name}'...")
    state["model"] = WhisperModel(model_name, device="cpu", compute_type="int8")
    log("Model ready.")

# ══════════════════════════════════════════════════════════════════════════════
#  macOS — Invisible background app (PyObjC)
# ══════════════════════════════════════════════════════════════════════════════

if IS_MAC:
    import objc
    import AppKit
    from Foundation import NSObject, NSMakeRect, NSTimer

    from overlay import RecordingOverlay
    from settings_window import SettingsWindow

    class AppDelegate(NSObject):

        def applicationDidFinishLaunching_(self, notification):
            log("App launched")

            # Hidden 1x1 off-screen window — keeps Carbon event loop alive
            # even when user closes the settings window
            self._keepAlive = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                NSMakeRect(-9999, -9999, 1, 1),
                AppKit.NSWindowStyleMaskBorderless,
                AppKit.NSBackingStoreBuffered,
                False,
            )
            self._keepAlive.setOpaque_(False)
            self._keepAlive.setBackgroundColor_(AppKit.NSColor.clearColor())
            self._keepAlive.setLevel_(-1)
            self._keepAlive.orderFront_(None)
            log("Keep-alive window created")

            # Audio stream
            try:
                start_audio_stream()
                log("Audio stream OK")
            except Exception as e:
                log(f"Audio stream FAILED: {e}")

            # Overlay (hidden until hotkey)
            self._overlay = RecordingOverlay()
            log("Overlay created")

            # Settings window
            self._settings_win = SettingsWindow(
                on_setting_changed=self._on_setting_changed,
                on_reload_dict=reload_dictionary,
            )
            self._settings_win.show()
            log("Settings window shown")

            # Load model in background
            threading.Thread(target=load_model, daemon=True).start()
            log("Model loading in background...")

            # Carbon hotkey (no Accessibility needed)
            self._pending_status = None
            try:
                start_hotkey_listener(update_ui=self._queue_status)
            except Exception as e:
                log(f"Hotkey FAILED: {e}")

            # Poll timer: check for status changes from hotkey thread
            NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                0.05, self, b"pollStatus:", None, True
            )
            log("App ready")

        def pollStatus_(self, timer):
            status = self._pending_status
            if status is not None:
                self._pending_status = None
                self._apply_status(status)

        @objc.python_method
        def _queue_status(self, status):
            """Called from any thread — safe."""
            log(f"_queue_status: {status}")
            self._pending_status = status

        @objc.python_method
        def _apply_status(self, status):
            """Runs on main thread."""
            log(f"_apply_status: {status}")
            if status == "recording":
                self._overlay.show("listening")
                log("overlay.show() called")
            elif status == "transcribing":
                self._overlay.set_status("transcribing")
            else:
                self._overlay.hide()

        @objc.python_method
        def _on_setting_changed(self, key, value):
            global settings
            settings[key] = value
            save_settings(settings)
            if key == "whisper_model":
                state["model"] = None
                threading.Thread(target=load_model, daemon=True).start()

        def applicationShouldTerminateAfterLastWindowClosed_(self, app):
            """Keep running in background when settings window is closed."""
            return False

        def applicationShouldHandleReopen_hasVisibleWindows_(self, app, flag):
            """When user clicks the app icon again, show settings."""
            self._settings_win.show()
            return True

    def run_mac():
        app = AppKit.NSApplication.sharedApplication()
        # Regular policy so Carbon hotkeys are delivered
        # LSUIElement in Info.plist hides the Dock icon
        app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)

        # Set app icon for when settings window is shown
        icon_path = os.path.join(RESOURCES_DIR, "icon_1024.png")
        if os.path.exists(icon_path):
            icon = AppKit.NSImage.alloc().initWithContentsOfFile_(icon_path)
            if icon:
                app.setApplicationIconImage_(icon)

        delegate = AppDelegate.alloc().init()
        app.setDelegate_(delegate)
        app.run()

# ══════════════════════════════════════════════════════════════════════════════
#  Windows — Invisible background app
# ══════════════════════════════════════════════════════════════════════════════

elif IS_WINDOWS:
    import tkinter as tk

    class WinOverlay:
        """Floating overlay for Windows using tkinter."""
        def __init__(self):
            self._root = tk.Tk()
            self._root.withdraw()
            self._win = tk.Toplevel(self._root)
            self._win.overrideredirect(True)
            self._win.attributes("-topmost", True)
            self._win.attributes("-alpha", 0.93)
            self._win.configure(bg="#1c2e1c")
            W, H = 360, 120
            sw = self._win.winfo_screenwidth()
            sh = self._win.winfo_screenheight()
            x = (sw - W) // 2
            y = int(sh * 0.3)
            self._win.geometry(f"{W}x{H}+{x}+{y}")
            # Title
            tk.Label(self._win, text="QStrauss", font=("Segoe UI", 18, "bold"),
                     fg="white", bg="#1c2e1c").pack(pady=(16, 4))
            # Status label
            self._label = tk.Label(self._win, text="Escuchando...",
                                   font=("Segoe UI", 12), fg="#66d966", bg="#1c2e1c")
            self._label.pack()
            # Hint
            self._hint = tk.Label(self._win, text="Ctrl+Space para detener",
                                  font=("Segoe UI", 9), fg="#5a7a5a", bg="#1c2e1c")
            self._hint.pack(pady=(4, 0))
            self._win.withdraw()

        def show(self, status="listening"):
            text = "Escuchando..." if status == "listening" else "Transcribiendo..."
            self._label.config(text=text)
            self._win.deiconify()
            self._win.lift()
            self._win.attributes("-topmost", True)

        def set_status(self, status):
            text = "Escuchando..." if status == "listening" else "Transcribiendo..."
            self._label.config(text=text)

        def hide(self):
            self._win.withdraw()

        def update(self):
            """Must be called periodically from main thread."""
            self._root.update()

    def run_windows():
        log("run_windows starting...")

        # Create main tkinter root first
        root = tk.Tk()
        root.title("QStrauss Voice")
        root.configure(bg="#1c2e1c")
        root.resizable(False, False)

        # Startup window — visible immediately
        W, H = 400, 200
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        x = (sw - W) // 2
        y = (sh - H) // 2
        root.geometry(f"{W}x{H}+{x}+{y}")
        root.attributes("-topmost", True)

        tk.Label(root, text="QStrauss Voice", font=("Segoe UI", 22, "bold"),
                 fg="white", bg="#1c2e1c").pack(pady=(30, 8))
        startup_label = tk.Label(root, text="Cargando modelo...",
                                 font=("Segoe UI", 12), fg="#66d966", bg="#1c2e1c")
        startup_label.pack()
        tk.Label(root, text="Esto puede tardar un momento la primera vez",
                 font=("Segoe UI", 9), fg="#5a7a5a", bg="#1c2e1c").pack(pady=(8, 0))

        root.update()
        log("Startup window shown")

        # Start audio and model loading
        start_audio_stream()

        def _load_and_notify():
            load_model()
            # Signal that model is ready
            _pending["model_ready"] = True

        threading.Thread(target=_load_and_notify, daemon=True).start()

        # Overlay window (hidden until hotkey)
        overlay_win = tk.Toplevel(root)
        overlay_win.overrideredirect(True)
        overlay_win.attributes("-topmost", True)
        overlay_win.attributes("-alpha", 0.93)
        overlay_win.configure(bg="#1c2e1c")
        OW, OH = 360, 120
        ox = (sw - OW) // 2
        oy = int(sh * 0.3)
        overlay_win.geometry(f"{OW}x{OH}+{ox}+{oy}")
        tk.Label(overlay_win, text="QStrauss", font=("Segoe UI", 18, "bold"),
                 fg="white", bg="#1c2e1c").pack(pady=(16, 4))
        overlay_label = tk.Label(overlay_win, text="Escuchando...",
                                 font=("Segoe UI", 12), fg="#66d966", bg="#1c2e1c")
        overlay_label.pack()
        overlay_hint = tk.Label(overlay_win, text="Ctrl+Space para detener",
                                font=("Segoe UI", 9), fg="#5a7a5a", bg="#1c2e1c")
        overlay_hint.pack(pady=(4, 0))
        overlay_win.withdraw()

        class _Overlay:
            def show(self, status="listening"):
                text = "Escuchando..." if status == "listening" else "Transcribiendo..."
                overlay_label.config(text=text)
                overlay_win.deiconify()
                overlay_win.lift()
                overlay_win.attributes("-topmost", True)
            def set_status(self, status):
                text = "Escuchando..." if status == "listening" else "Transcribiendo..."
                overlay_label.config(text=text)
            def hide(self):
                overlay_win.withdraw()

        overlay = _Overlay()

        # Status callback
        _pending = {"status": None, "model_ready": False}

        def queue_status(s):
            _pending["status"] = s

        start_hotkey_listener(update_ui=queue_status)
        log("Hotkey listener started")

        # System tray icon
        try:
            import pystray
            from PIL import Image as PILImage
            icon_path = os.path.join(RESOURCES_DIR, "icon_1024.png")
            if os.path.exists(icon_path):
                tray_image = PILImage.open(icon_path).resize((64, 64))
            else:
                tray_image = PILImage.new("RGB", (64, 64), "#1c2e1c")

            def on_quit(icon, item):
                icon.stop()
                os._exit(0)

            tray = pystray.Icon(
                "QStrauss Voice",
                tray_image,
                "QStrauss Voice - Ctrl+Space",
                menu=pystray.Menu(pystray.MenuItem("Salir", on_quit)),
            )
            threading.Thread(target=tray.run, daemon=True).start()
            log("System tray icon created")
        except Exception as e:
            log(f"Tray icon error (non-fatal): {e}")

        # Main loop
        def poll():
            # Check if model finished loading
            if _pending["model_ready"]:
                _pending["model_ready"] = False
                startup_label.config(text="Listo! Presiona Ctrl+Space")
                log("Model ready — updating startup window")
                # Auto-hide startup window after 2 seconds
                root.after(2000, root.withdraw)

            # Apply recording status changes
            s = _pending["status"]
            if s is not None:
                _pending["status"] = None
                log(f"_apply_status: {s}")
                if s == "recording":
                    overlay.show("listening")
                elif s == "transcribing":
                    overlay.set_status("transcribing")
                else:
                    overlay.hide()

            root.after(50, poll)

        root.after(50, poll)

        # Handle window close — hide to tray instead of quitting
        def on_close():
            root.withdraw()
        root.protocol("WM_DELETE_WINDOW", on_close)

        log("Entering main loop")
        root.mainloop()

# ─── Main ────────────────────────────────────────────────────────────────────

LOG_FILE = os.path.join(BASE_DIR, "app.log")

def log(msg):
    """Write to both stdout and log file for debugging."""
    print(msg, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")

def main():
    # Clear log
    with open(LOG_FILE, "w") as f:
        f.write("")
    log(f"{APP_NAME} starting...")
    log(f"Hotkey: {settings.get('hotkey_display', '⌥ Space')}")

    reload_dictionary()

    if IS_MAC:
        run_mac()
    elif IS_WINDOWS:
        run_windows()

if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    main()
