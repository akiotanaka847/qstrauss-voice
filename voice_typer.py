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
    "whisper_model": "turbo",
    "language": "auto",
    "microphone": "default",
    "hotkey_mod": "alt" if IS_MAC else "ctrl",
    "hotkey_key": "space",
    "hotkey_display": "⌥ Space" if IS_MAC else "Ctrl + Space",
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
            beam_size=1,
            temperature=0,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 300, "speech_pad_ms": 200},
            condition_on_previous_text=False,
            no_speech_threshold=0.6,
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

    _WIN_MOD_MAP = {
        "ctrl": (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r),
        "control": (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r),
        "alt": (keyboard.Key.alt_l, keyboard.Key.alt_r),
        "option": (keyboard.Key.alt_l, keyboard.Key.alt_r),
        "shift": (keyboard.Key.shift_l, keyboard.Key.shift_r),
        "cmd": (keyboard.Key.cmd,), "command": (keyboard.Key.cmd,),
    }

    _WIN_KEY_MAP = {
        "space": keyboard.Key.space, "return": keyboard.Key.enter,
        "tab": keyboard.Key.tab, "escape": keyboard.Key.esc,
    }
    for i in range(1, 13):
        _WIN_KEY_MAP[f"f{i}"] = getattr(keyboard.Key, f"f{i}")

    mod_keys = _WIN_MOD_MAP.get(mod_name.lower(), (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r))
    main_key = _WIN_KEY_MAP.get(key_name.lower())
    if main_key is None:
        try:
            main_key = keyboard.KeyCode.from_char(key_name.lower())
        except Exception:
            main_key = keyboard.Key.space

    pressed_keys = set()
    _last_fire = [0.0]

    def on_press(key):
        pressed_keys.add(key)
        mod_held = any(mk in pressed_keys for mk in mod_keys)
        if mod_held and key == main_key:
            now = time.time()
            if now - _last_fire[0] > 0.4:   # debounce 400 ms
                _last_fire[0] = now
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
    model_name = settings.get("whisper_model", "turbo")
    cpu_threads = max(4, os.cpu_count() or 4)
    log(f"Loading Whisper '{model_name}' (cpu_threads={cpu_threads})...")
    state["model"] = WhisperModel(
        model_name,
        device="cpu",
        compute_type="int8",
        cpu_threads=cpu_threads,
        num_workers=1,
    )
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
        # Force process name BEFORE NSApplication so macOS menu bar shows
        # "QStrauss Voice" instead of "Python"
        import ctypes
        try:
            ctypes.cdll.LoadLibrary("libc.dylib").setprogname(b"QStrauss Voice")
        except Exception:
            pass
        from Foundation import NSProcessInfo
        NSProcessInfo.processInfo().setProcessName_("QStrauss Voice")

        app = AppKit.NSApplication.sharedApplication()
        app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)

        # Build menu bar explicitly
        menubar = AppKit.NSMenu.alloc().init()
        app_item = AppKit.NSMenuItem.alloc().init()
        menubar.addItem_(app_item)
        app.setMainMenu_(menubar)

        app_menu = AppKit.NSMenu.alloc().initWithTitle_("QStrauss Voice")
        app_menu.addItem_(
            AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "Acerca de QStrauss Voice", "orderFrontStandardAboutPanel:", ""
            )
        )
        app_menu.addItem_(AppKit.NSMenuItem.separatorItem())
        app_menu.addItem_(
            AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                "Salir de QStrauss Voice", "terminate:", "q"
            )
        )
        app_item.setSubmenu_(app_menu)

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
#  Windows — pywebview-based app (same HTML/CSS as Mac)
# ══════════════════════════════════════════════════════════════════════════════

elif IS_WINDOWS:
    import ctypes
    from settings_window_win import SettingsApi

    def _hide_console():
        try:
            hwnd = ctypes.windll.kernel32.GetConsoleWindow()
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 0)
        except Exception:
            pass

    def _set_win32_icon():
        """Set QStrauss .ico on all top-level windows via Win32 enumeration."""
        try:
            ico_path = os.path.join(RESOURCES_DIR, "QStraussVoice.ico")
            LR_LOADFROMFILE = 0x0010
            IMAGE_ICON = 1
            hicon = ctypes.windll.user32.LoadImageW(
                None, ico_path, IMAGE_ICON, 0, 0, LR_LOADFROMFILE
            )
            if not hicon:
                log("LoadImageW returned null — .ico not found?")
                return
            WM_SETICON = 0x0080
            buf = ctypes.create_unicode_buffer(256)

            def _enum_cb(hwnd, lparam):
                ctypes.windll.user32.GetWindowTextW(hwnd, buf, 256)
                if "QStrauss Voice" in buf.value:
                    ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, 1, hicon)  # ICON_BIG
                    ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, 0, hicon)  # ICON_SMALL
                return True

            WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
            ctypes.windll.user32.EnumWindows(WNDENUMPROC(_enum_cb), 0)
            log("Win32 icons applied")
        except Exception as e:
            log(f"Win32 icon error (non-fatal): {e}")

    def run_windows():
        import webview

        log("run_windows starting...")

        # Fix taskbar name + icon: must be called BEFORE any window is created
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("QStrauss.Voice")
            log("AppUserModelID set: QStrauss.Voice")
        except Exception as e:
            log(f"AppUserModelID error (non-fatal): {e}")

        _hide_console()

        # Screen size via Win32 (no tkinter needed)
        ctypes.windll.user32.SetProcessDPIAware()
        sw = ctypes.windll.user32.GetSystemMetrics(0)
        sh = ctypes.windll.user32.GetSystemMetrics(1)

        _pending = {"status": None, "model_ready": False}

        def _on_setting_changed(key, value):
            global settings
            settings[key] = value
            if key == "whisper_model":
                state["model"] = None
                threading.Thread(target=load_model, daemon=True).start()

        api = SettingsApi(
            on_setting_changed=_on_setting_changed,
            on_reload_dict=reload_dictionary,
        )

        SETTINGS_HTML = os.path.join(RESOURCES_DIR, "settings.html")

        # Overlay HTML — transparent window with rounded navy card (same look as Mac)
        OW, OH = 320, 120
        OVERLAY_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:320px;height:120px;background:transparent;overflow:hidden}
.card{
  position:fixed;inset:0;
  background:rgba(11,17,51,0.96);
  border-radius:18px;
  display:flex;flex-direction:column;
  align-items:center;justify-content:center;gap:2px;
  box-shadow:0 8px 32px rgba(0,0,0,.5);
}
.border-line{position:absolute;top:0;left:32px;right:32px;height:1.5px;background:#00c896;border-radius:1px}
.title{font-family:'Segoe UI',sans-serif;font-size:15px;font-weight:700;color:#fff;letter-spacing:-.2px}
.title span{color:#00c896;font-weight:400}
.hint{font-family:'Segoe UI',sans-serif;font-size:9px;color:#3a4e72;margin-top:1px}
</style></head><body>
<div class="card">
  <div class="border-line"></div>
  <div class="title">QStrauss<span> Voice</span></div>
  <canvas id="c" width="280" height="36"></canvas>
  <div class="hint" id="hint">Presiona el atajo para detener</div>
</div>
<script>
var status='listening',phase=0;
var cv=document.getElementById('c'),ctx=cv.getContext('2d');
function draw(){
  ctx.clearRect(0,0,280,36);
  var cy=18;
  if(status==='listening'){
    for(var i=0;i<18;i++){
      var dy=Math.sin(phase*.10+i*.38)*5,al=.5+.5*Math.abs(Math.sin(phase*.08+i*.3));
      ctx.fillStyle='rgba(0,200,150,'+al+')';
      ctx.beginPath();ctx.arc(14+i*(252/17),cy+dy,3.5,0,Math.PI*2);ctx.fill();
    }
  }else{
    for(var i=0;i<3;i++){
      var ph=i*(Math.PI*2/3),dy=Math.sin(phase*.14+ph)*6,al=.5+.5*Math.abs(Math.sin(phase*.14+ph));
      ctx.fillStyle='rgba(0,200,150,'+al+')';
      ctx.beginPath();ctx.arc(140+(i-1)*20,cy+dy,5,0,Math.PI*2);ctx.fill();
    }
  }
  phase++;requestAnimationFrame(draw);
}
draw();
function setStatus(s){
  status=s;
  document.getElementById('hint').textContent=s==='listening'?'Presiona el atajo para detener':'Un momento…';
}
</script></body></html>"""

        ox = (sw - OW) // 2
        oy = int(sh * 0.72)

        # Settings window starts hidden — shown only after HTML page is loaded
        settings_win = webview.create_window(
            "QStrauss Voice",
            SETTINGS_HTML,
            width=500, height=720,
            resizable=False,
            js_api=api,
            hidden=True,
        )
        api._win = settings_win

        overlay_win = webview.create_window(
            "QStrauss Voice Overlay",
            html=OVERLAY_HTML,
            x=ox, y=oy,
            width=OW, height=OH,
            frameless=True,
            transparent=True,
            on_top=True,
            hidden=True,
        )

        # Hide instead of close so the app keeps running via tray
        def _on_settings_closing():
            settings_win.hide()
            return False
        settings_win.events.closing += _on_settings_closing

        # Show settings only once the page has fully loaded
        _settings_loaded = {"done": False}
        def _on_settings_loaded():
            if not _settings_loaded["done"]:
                _settings_loaded["done"] = True
                api.inject_settings()
                if not settings.get("start_hidden", False):
                    settings_win.show()
        settings_win.events.loaded += _on_settings_loaded

        def on_start():
            log("webview on_start")

            start_audio_stream()

            def _load_and_notify():
                load_model()
                _pending["model_ready"] = True
            threading.Thread(target=_load_and_notify, daemon=True).start()

            def queue_status(s):
                _pending["status"] = s
            start_hotkey_listener(update_ui=queue_status)

            # Poll thread: model ready + overlay status
            def _poll():
                while True:
                    if _pending["model_ready"]:
                        _pending["model_ready"] = False
                        log("Model ready")
                        api.inject_settings(extra={"model_ready": True})

                    s = _pending["status"]
                    if s is not None:
                        _pending["status"] = None
                        try:
                            if s == "recording":
                                overlay_win.show()
                                overlay_win.evaluate_js("setStatus('listening')")
                            elif s == "transcribing":
                                overlay_win.evaluate_js("setStatus('transcribing')")
                            else:
                                overlay_win.hide()
                        except Exception as e:
                            log(f"overlay error: {e}")
                    time.sleep(0.05)
            threading.Thread(target=_poll, daemon=True).start()

            # System tray
            try:
                import pystray
                from PIL import Image as PILImage
                icon_path = os.path.join(RESOURCES_DIR, "icon_1024.png")
                tray_img = (PILImage.open(icon_path).resize((64, 64))
                            if os.path.exists(icon_path)
                            else PILImage.new("RGB", (64, 64), "#0b1133"))

                def on_show(icon, item):
                    settings_win.show()

                def on_quit(icon, item):
                    icon.stop()
                    os._exit(0)

                hotkey_display = settings.get("hotkey_display", "Ctrl + Space")
                tray = pystray.Icon(
                    "QStrauss Voice", tray_img,
                    f"QStrauss Voice — {hotkey_display}",
                    menu=pystray.Menu(
                        pystray.MenuItem("Mostrar", on_show, default=True),
                        pystray.MenuItem("Salir", on_quit),
                    ),
                )
                threading.Thread(target=tray.run, daemon=True).start()
                log("Tray icon created")
            except Exception as e:
                log(f"Tray error: {e}")

            # Apply QStrauss icon to all open windows after a short delay
            # (windows may not yet have handles at on_start time)
            def _apply_icon_delayed():
                time.sleep(1.5)
                _set_win32_icon()
            threading.Thread(target=_apply_icon_delayed, daemon=True).start()

        log("Starting webview main loop")
        webview.start(on_start, debug=False)

# ─── Main ────────────────────────────────────────────────────────────────────

LOG_FILE = os.path.join(BASE_DIR, "app.log")

def log(msg):
    """Write to both stdout and log file for debugging."""
    print(msg, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")

LOCK_FILE = os.path.join(BASE_DIR, "app.lock")

def _acquire_lock():
    """Single-instance lock. Returns False if another instance is already running."""
    if not IS_WINDOWS:
        return True
    import ctypes
    mutex = ctypes.windll.kernel32.CreateMutexW(None, True, "QStraussVoice_SingleInstance")
    err = ctypes.windll.kernel32.GetLastError()
    if err == 183:  # ERROR_ALREADY_EXISTS
        return False
    _hotkey_refs.append(mutex)  # keep reference alive
    return True

def main():
    if IS_WINDOWS and not _acquire_lock():
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            0, "QStrauss Voice ya está corriendo.", "QStrauss Voice", 0x40
        )
        return

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
