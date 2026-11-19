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
import time
import subprocess
import numpy as np
import sounddevice as sd
import pyperclip
from faster_whisper import WhisperModel

import vad
import terms
import learn

IS_MAC     = sys.platform == "darwin"
IS_WINDOWS = sys.platform == "win32"

APP_NAME        = "QStrauss Voice"
BASE_DIR        = os.path.dirname(os.path.abspath(__file__))


def _dir_datos_usuario():
    """Dónde guardar lo que el usuario modifica.

    Empaquetada, BASE_DIR apunta DENTRO del .app. Escribir ahí tiene tres
    problemas: puede invalidar la firma de código, se borra en cada
    reinstalación (el usuario pierde sus ajustes), y en algunos equipos
    /Applications no es escribible. Los datos del usuario van a
    la carpeta estándar de cada sistema (Application Support en macOS,
    %APPDATA% en Windows) y solo lo de solo-lectura queda en el bundle.
    """
    if getattr(sys, "frozen", False):
        if IS_WINDOWS:
            base = os.environ.get("APPDATA") or os.path.expanduser("~")
            d = os.path.join(base, APP_NAME)
        else:
            d = os.path.expanduser("~/Library/Application Support/" + APP_NAME)
        try:
            os.makedirs(d, exist_ok=True)
            return d
        except Exception:
            return BASE_DIR
    return BASE_DIR


DATA_DIR = _dir_datos_usuario()


def _archivo_de_usuario(nombre):
    """Ruta en DATA_DIR, sembrada desde el bundle la primera vez.

    Así el diccionario queda editable por el usuario en vez de enterrado
    dentro del paquete, y sobrevive a las reinstalaciones.
    """
    destino = os.path.join(DATA_DIR, nombre)
    origen = os.path.join(BASE_DIR, nombre)
    if not os.path.exists(destino) and os.path.exists(origen) and destino != origen:
        try:
            import shutil
            shutil.copy2(origen, destino)
        except Exception:
            return origen
    return destino


DICTIONARY_FILE = _archivo_de_usuario("dictionary.json")
RESOURCES_DIR   = os.path.join(BASE_DIR, "resources")
SETTINGS_FILE   = os.path.join(DATA_DIR, "settings.json")
SAMPLE_RATE     = 16000
# Tamaño de bloque del stream de audio, en muestras.
#
# Sin esto sounddevice pasa blocksize=0 y deja que el driver elija. CoreAudio
# eligió 15 frames (0.94 ms), lo que dispara el callback de Python MÁS DE MIL
# VECES POR SEGUNDO, cada una tomando el GIL desde un hilo de audio en tiempo
# real, y de forma permanente porque el stream nunca se cierra. Medido: 8363
# callbacks en 7.8 s de grabación.
#
# 1600 muestras son 100 ms: 10 callbacks por segundo en vez de 1072, unas 100
# veces menos trabajo. No afecta la latencia percibida porque la grabación se
# corta al pulsar el atajo, no por bloque, y el VAD ya recorta los bordes.
BLOCK_SIZE      = 1600
# Cuánto se queda el aviso "sin voz" en pantalla antes de cerrarse solo.
NO_SPEECH_FLASH_S = 1.4

# ─── Settings ────────────────────────────────────────────────────────────────

DEFAULT_SETTINGS = {
    "whisper_model": "small",
    "language": "auto",
    "microphone": "default",
    "hotkey_mod": "alt" if IS_MAC else "ctrl",
    "hotkey_key": "space",
    "hotkey_display": "⌥ Space" if IS_MAC else "Ctrl + Space",
    "trailing_space": True,
    "paste_mode": "clipboard_paste",
    "vad_enabled": True,
    "vad_threshold": 0.65,
    "vad_min_speech_ms": 200,
    "vad_speech_pad_ms": 200,
    "vad_min_silence_ms": 300,
    "fuzzy_terms": True,
    "aprender_correcciones": True,
    "hotkey_aprender_mod": "alt",
    "hotkey_aprender_key": "l",
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
    "term_list":      [],
    "lexico":         None,
    "transcribing":   False,
    # Contador de generación. Cancelar lo incrementa; la transcripción en
    # vuelo compara contra el valor que capturó al empezar y se descarta si
    # cambió. Evita la carrera de cancelar mientras el modelo ya corre.
    "gen":            0,
    "model_error":    None,
    "ultimo_uso":     0.0,
    "descargado":     False,
    # Última transcripción, SOLO en memoria. Nunca toca el disco: es lo
    # que permite aprender de una corrección sin guardar historial.
    "ultima":         "",
    "model":          None,
    "backend":        "faster_whisper",  # "mlx" on Apple Silicon, "faster_whisper" on Windows
}
lock = threading.Lock()

# ─── Dictionary ──────────────────────────────────────────────────────────────

def load_dictionary():
    if not os.path.exists(DICTIONARY_FILE):
        return {}, "", []
    with open(DICTIONARY_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    corrections    = {k.lower(): v for k, v in data.get("corrections", {}).items()}
    hints          = data.get("hints", [])
    initial_prompt = ", ".join(hints)
    # Los hints son a la vez el sesgo del decoder y el corpus contra el que se
    # comparan los términos mal oídos. Se agregan los destinos de las
    # correcciones exactas para no tener que repetirlos en las dos listas.
    term_list      = list(dict.fromkeys(hints + list(corrections.values())))
    return corrections, initial_prompt, term_list

def reload_dictionary():
    c, p, t = load_dictionary()
    state["corrections"]    = c
    state["initial_prompt"] = p
    state["term_list"]      = t
    if state["lexico"] is None:
        state["lexico"] = terms.Lexico()
    print(f"Dictionary: {len(c)} corrections, {len(t)} terms, "
          f"lexico={'si' if state['lexico'].disponible else 'no'}")

# ─── Sound effects ───────────────────────────────────────────────────────────

SFX_START = "/System/Library/Sounds/Pop.aiff"
SFX_STOP  = "/System/Library/Sounds/Tink.aiff"

def play_sfx(path):
    if IS_MAC and os.path.exists(path):
        subprocess.Popen(["afplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

# ─── Audio stream ────────────────────────────────────────────────────────────

audio_stream = None

_audio_cb_count = 0

_audio_cb_status = [None]

def audio_callback(indata, frames, time_info, status):
    """Callback de audio en tiempo real. Debe ser lo más barato posible.

    Nada de I/O de disco aquí: `log()` abre y cierra el archivo en cada
    llamada, y hacer eso desde un hilo de audio de tiempo real bloquea la
    entrega de buffers. El estado se guarda en una variable y lo registra
    quien pare la grabación.
    """
    global _audio_cb_count
    _audio_cb_count += 1
    if status:
        _audio_cb_status[0] = str(status)
    if state["recording"]:
        state["audio_chunks"].append(indata.copy())

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
            blocksize=BLOCK_SIZE,
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
                blocksize=BLOCK_SIZE,
            )
            audio_stream.start()
            log(f"Audio stream ready (default fallback): active={audio_stream.active}")
        except Exception as e2:
            log(f"Audio stream FATAL: {e2}")

# ─── Language ────────────────────────────────────────────────────────────────

def current_language():
    lang = settings.get("language", "auto")
    return None if lang == "auto" else lang

# ─── Aprender de una corrección ──────────────────────────────────────────────

def aprender_de_portapapeles(update_ui=None):
    """Compara el portapapeles con la última transcripción y aprende.

    Flujo pensado para no interrumpir: dictas, corriges el texto donde ya
    estás, lo seleccionas, copias y pulsas el atajo. No hace falta abrir nada
    ni volver a la app.
    """
    original = (state.get("ultima") or "").strip()
    if not original:
        log("Aprender: no hay transcripción reciente en memoria")
        if update_ui:
            update_ui("sin_correccion")
        return

    try:
        corregido = (pyperclip.paste() or "").strip()
    except Exception as e:
        log("Aprender: no se pudo leer el portapapeles: %s" % e)
        return

    if not corregido:
        log("Aprender: el portapapeles está vacío")
        if update_ui:
            update_ui("sin_correccion")
        return
    if terms.clave(corregido) == terms.clave(original):
        log("Aprender: el portapapeles es igual a lo transcrito, nada que aprender")
        if update_ui:
            update_ui("sin_correccion")
        return

    try:
        aprendibles, descartados = learn.extraer_correcciones(
            original, corregido, state.get("lexico"))
    except Exception as e:
        log("Aprender: error comparando: %s" % e)
        return

    for d in descartados:
        log("  no se aprende: %s -> %s (%s)" % (d["oido"], d["corregido"], d["motivo"]))

    if not aprendibles:
        log("Aprender: ninguna diferencia califica como corrección de término")
        if update_ui:
            update_ui("sin_correccion")
        return

    try:
        nuevas, ya = learn.aplicar(aprendibles, DICTIONARY_FILE)
    except Exception as e:
        log("Aprender: no se pudo escribir el diccionario: %s" % e)
        return

    if nuevas:
        for c in nuevas:
            log("APRENDIDO: %s -> %s (%s)" % (c["oido"], c["corregido"], c["motivo"]))
        reload_dictionary()   # aplica desde el siguiente dictado, sin reiniciar
        if update_ui:
            update_ui("aprendido")
    else:
        log("Aprender: esas correcciones ya estaban en el diccionario")
        if update_ui:
            update_ui("sin_correccion")


# ─── Foco y pegado ───────────────────────────────────────────────────────────
#
# La app pega con un Cmd+V sintético, así que el texto cae en lo que tenga el
# foco EN ESE MOMENTO, no en lo que lo tenía cuando empezaste a hablar. Basta
# con que la ventana de ajustes esté al frente para que el dictado aterrice
# ahí. Por eso se guarda la app de destino al empezar a grabar y se le devuelve
# el foco justo antes de pegar.

_foco = {"app": None}

def _recordar_foco():
    """Guarda qué aplicación tenía el foco al empezar a grabar."""
    if not IS_MAC:
        return
    try:
        import AppKit
        app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        # Si el frente es esta misma app (ventana de ajustes), no hay destino
        # externo que restaurar: pegar ahí sería el bug que queremos evitar.
        if app is None or app.processIdentifier() == os.getpid():
            _foco["app"] = None
            log("Foco: la app propia está al frente, no hay destino externo")
        else:
            _foco["app"] = app
            log("Foco guardado: %s" % app.localizedName())
    except Exception as e:
        _foco["app"] = None
        log("No se pudo guardar el foco: %s" % e)

def _restaurar_foco():
    """Devuelve el foco a la app que lo tenía al empezar a grabar."""
    if not IS_MAC:
        return True
    app = _foco.get("app")
    if app is None:
        return False
    try:
        import AppKit
        actual = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        if actual is not None and actual.processIdentifier() == app.processIdentifier():
            return True   # ya está al frente, no hace falta nada
        NSApplicationActivateIgnoringOtherApps = 1 << 1
        app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
        time.sleep(0.12)  # darle tiempo a venir al frente antes del Cmd+V
        log("Foco devuelto a %s" % app.localizedName())
        return True
    except Exception as e:
        log("No se pudo devolver el foco: %s" % e)
        return False

def accesibilidad_ok(pedir=False):
    """True si el proceso puede sintetizar teclas (permiso de Accesibilidad).

    Es un chequeo obligatorio, no un lujo: `CGEventPost` NO falla ni lanza
    excepción cuando falta el permiso, simplemente descarta el evento. Sin
    esta comprobación la app registra "pegado enviado" y no pega nada, que es
    la peor forma de fallar: silenciosa y con el log mintiendo.
    """
    if not IS_MAC:
        return True
    try:
        import ApplicationServices as AS
        if pedir:
            return bool(AS.AXIsProcessTrustedWithOptions(
                {AS.kAXTrustedCheckOptionPrompt: True}))
        return bool(AS.AXIsProcessTrusted())
    except Exception as e:
        log("No se pudo consultar Accesibilidad: %s" % e)
        return True   # ante la duda, intentar pegar

def _pegar_en_cursor():
    """Envía Cmd+V (o Ctrl+V) al destino que tenga el foco.

    En macOS usa CGEventPost en vez de osascript: el AppleScript levanta un
    intérprete entero por cada pegado (~270 ms medidos) y agrega un subproceso
    que puede fallar por timeout. El evento nativo es inmediato. Ambos caminos
    necesitan el mismo permiso de Accesibilidad.
    """
    if IS_MAC:
        import Quartz
        kVK_ANSI_V = 9
        src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
        for abajo in (True, False):
            ev = Quartz.CGEventCreateKeyboardEvent(src, kVK_ANSI_V, abajo)
            Quartz.CGEventSetFlags(ev, Quartz.kCGEventFlagMaskCommand)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
    else:
        from pynput import keyboard as kb
        ctrl = kb.Controller()
        with ctrl.pressed(kb.Key.ctrl):
            ctrl.press('v')
            ctrl.release('v')

# ─── Recording ───────────────────────────────────────────────────────────────

def start_recording(update_ui=None):
    global _audio_cb_count
    log(f"start_recording called (audio_cb_count={_audio_cb_count}, stream_active={audio_stream.active if audio_stream else 'None'})")
    if state["model"] is None and state.get("descargado"):
        log("Modelo descargado por inactividad, recargando")
        state["descargado"] = False
        threading.Thread(target=load_model, daemon=True).start()
        if update_ui:
            update_ui("cargando")
        return

    if state["model"] is None:
        err = state.get("model_error")
        if err:
            log("No se puede grabar: el modelo falló al cargar (%s)" % err)
        else:
            log("El modelo todavía se está cargando, espera unos segundos")
        # Pulsar el atajo y que no pase absolutamente nada es indistinguible de
        # que la app esté colgada. El overlay lo dice.
        if update_ui:
            update_ui("cargando")
        return
    with lock:
        if state["recording"]:
            return
        state["recording"]    = True
        state["audio_chunks"] = []
    _audio_cb_count = 0
    _recordar_foco()
    _register_escape()
    play_sfx(SFX_START)
    log("Recording started — speak now")
    if update_ui:
        update_ui("recording")

def stop_and_transcribe(update_ui=None):
    """Envoltorio: garantiza que el estado quede limpio por cualquier salida.

    El cuerpo tiene varias salidas tempranas (sin audio, silencio, error del
    modelo, cancelación). Sin el finally, cualquiera de ellas dejaría
    `transcribing` en True y Escape registrado para siempre.
    """
    try:
        _stop_and_transcribe(update_ui=update_ui)
    finally:
        with lock:
            state["transcribing"] = False
        _unregister_escape()


def _stop_and_transcribe(update_ui=None):
    with lock:
        if not state["recording"]:
            return
        state["recording"] = False
        state["transcribing"] = True
        mi_gen = state["gen"]
        chunks = list(state["audio_chunks"])
        state["audio_chunks"] = []
    # Escape sigue vivo durante la transcripción: es cuando el usuario se da
    # cuenta de que dijo algo mal. Se libera en el finally del envoltorio.

    log(f"stop_and_transcribe: {len(chunks)} chunks, audio_cb_count={_audio_cb_count}")
    if _audio_cb_status[0]:
        log("Avisos del stream de audio durante la grabación: %s" % _audio_cb_status[0])
        _audio_cb_status[0] = None
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

    # Algunos micrófonos, con ganancia alta o conversión de frecuencia de por
    # medio, entregan float32 fuera del rango [-1, 1]. No es pérdida de
    # información (float32 lo representa bien), pero a ganancias extremas
    # degrada la detección de voz. Medido sobre la misma frase: a x8 el VAD
    # recupera 4.30s en vez de 5.83s; normalizando vuelve a 5.80s. A x2 y x4
    # no cambia nada, así que normalizar es gratis.
    if peak > 1.0:
        log("Audio por encima de rango (pico %.2f), normalizando" % peak)
        audio = (audio / peak).astype(np.float32)
        peak = 1.0

    # ── VAD: quitar el silencio ANTES de que Whisper lo alucine ──────────────
    # Se aplica aquí, fuera del modelo, para que MLX y faster-whisper reciban
    # exactamente el mismo audio. mlx_whisper.transcribe() no acepta vad_filter.
    if settings.get("vad_enabled", True) and vad.is_available():
        speech, vstats = vad.analyze(audio, settings, SAMPLE_RATE)
        if not vstats["ok"]:
            log("VAD omitido: %s" % vstats["reason"])
        else:
            log("VAD: %d segmento(s), %.2fs de voz en %.2fs (%.0f%%)" % (
                vstats["segments"], vstats["speech_s"],
                vstats["original_s"], vstats["ratio"] * 100))
            chosen, why = vad.decide(
                speech, vstats, audio, peak, rms, settings, SAMPLE_RATE)
            if chosen is None:
                # Abortar en silencio deja al usuario sin saber qué pasó, así
                # que el overlay lo dice antes de cerrarse.
                log("No se transcribe: %s" % why)
                if update_ui:
                    update_ui("no_speech")
                return
            audio = np.asarray(chosen, dtype=np.float32).flatten()
            log("Audio a transcribir: %.2fs [%s]" % (len(audio) / SAMPLE_RATE, why))

    t0 = time.time()
    try:
        if state.get("backend") == "mlx":
            import mlx_whisper
            result = mlx_whisper.transcribe(
                audio,
                path_or_hf_repo=state["model"],
                language=current_language(),
                verbose=False,
                word_timestamps=False,
                initial_prompt=state["initial_prompt"] or None,
            )
            text = result.get("text", "").strip()
            lang = result.get("language", "?")
            log(f"MLX result: '{text}' (lang={lang}) in {time.time()-t0:.1f}s")
        else:
            segs, info = state["model"].transcribe(
                audio,
                language=current_language(),
                beam_size=1,
                temperature=0,
                vad_filter=False,  # ya se filtró arriba con vad.analyze()
                condition_on_previous_text=False,
                no_speech_threshold=0.6,
                initial_prompt=state["initial_prompt"] or None,
            )
            text = " ".join(s.text for s in segs).strip()
            log(f"Whisper result: '{text}' (lang={info.language}, prob={info.language_probability:.2f}) in {time.time()-t0:.1f}s")
    except Exception as e:
        log(f"Transcription ERROR: {e}")
        if update_ui:
            update_ui("idle")
        return

    if update_ui:
        update_ui("idle")

    if not text:
        log("No speech detected")
        return

    # Dos capas complementarias. Las exactas van primero y siempre ganan:
    # atrapan errores FONÉTICOS que la distancia de edición no alcanza
    # ("eicher" -> "Azure" son 5 ediciones sobre 6 letras). Las difusas
    # atrapan errores ORTOGRÁFICOS y palabras partidas ("work front").
    text = terms.aplicar_exactas(text, state["corrections"])
    if settings.get("fuzzy_terms", True):
        text, cambios = terms.aplicar_difusas(
            text, state["term_list"], state["lexico"])
        if cambios:
            log("Términos corregidos: %s" % "; ".join(cambios))

    if settings.get("trailing_space", True):
        text += " "

    state["ultimo_uso"] = time.time()
    state["ultima"] = text.strip()
    log(f"Transcribed: {text.strip()}")

    with lock:
        if mi_gen != state["gen"]:
            log("Transcripción descartada: se canceló mientras corría el modelo")
            return

    pyperclip.copy(text)

    if settings.get("paste_mode") == "clipboard_only":
        log("Modo 'solo portapapeles': no se pega automáticamente")
        if update_ui:
            update_ui("portapapeles")
        return

    hay_destino = _restaurar_foco()
    if not hay_destino and IS_MAC:
        # Nadie externo tenía el foco al empezar (típicamente la ventana de
        # ajustes estaba al frente). Pegar aquí metería el dictado en la propia
        # app, así que se deja en el portapapeles y se avisa.
        log("Sin destino externo: el texto quedó en el portapapeles, pega con Cmd+V")
        if update_ui:
            update_ui("portapapeles")
        return

    if not accesibilidad_ok():
        log("SIN PERMISO DE ACCESIBILIDAD: no se puede pegar automáticamente. "
            "El texto quedó en el portapapeles. Concede el permiso en "
            "Ajustes del Sistema > Privacidad y seguridad > Accesibilidad.")
        if update_ui:
            update_ui("sin_permiso")
        return

    time.sleep(0.08)
    try:
        _pegar_en_cursor()
        log("Pegado enviado")
    except Exception as e:
        log("Error al pegar: %s (el texto está en el portapapeles)" % e)

def cancel_recording(update_ui=None):
    """Descarta la grabación en curso sin transcribir ni pegar nada."""
    with lock:
        if not (state["recording"] or state["transcribing"]):
            return False
        grabando = state["recording"]
        state["recording"] = False
        n = len(state["audio_chunks"])
        state["audio_chunks"] = []
        state["gen"] += 1          # invalida cualquier transcripción en vuelo
    _unregister_escape()
    play_sfx(SFX_STOP)
    log("Cancelado durante %s (%d chunks descartados)" % (
        "la grabación" if grabando else "la transcripción", n))
    if update_ui:
        update_ui("idle")
    return True

def toggle_recording(update_ui=None):
    log(f"toggle_recording: recording={state['recording']}")
    if state["recording"]:
        threading.Thread(
            target=stop_and_transcribe, kwargs={"update_ui": update_ui}, daemon=True
        ).start()
    else:
        start_recording(update_ui=update_ui)

# ─── Cancelar con Escape ─────────────────────────────────────────────────────
#
# Escape NO se registra de forma permanente: eso le robaría la tecla a todas
# las apps del sistema. Se registra al empezar a grabar y se libera al parar,
# así solo existe durante los segundos en que tiene sentido.

_escape_state = {"registrar": None, "liberar": None}

def _register_escape():
    fn = _escape_state["registrar"]
    if fn:
        try:
            fn()
        except Exception as e:
            log("No se pudo registrar Escape: %s" % e)

def _unregister_escape():
    fn = _escape_state["liberar"]
    if fn:
        try:
            fn()
        except Exception as e:
            log("No se pudo liberar Escape: %s" % e)

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
    carbon.UnregisterEventHotKey.argtypes = [c_void_p]
    carbon.UnregisterEventHotKey.restype = c_int32
    # GetEventParameter permite saber CUÁL hotkey disparó. Sin esto, el
    # handler es único y no distingue el atajo principal de Escape.
    carbon.GetEventParameter.argtypes = [
        c_void_p, c_uint32, c_uint32, c_void_p, c_uint32, c_void_p, c_void_p,
    ]
    carbon.GetEventParameter.restype = c_int32
    carbon.GetEventKind.argtypes = [c_void_p]
    carbon.GetEventKind.restype = c_uint32

    EventHandlerProc = CFUNCTYPE(c_int32, c_void_p, c_void_p, c_void_p)

    kEventHotKeyReleased = 6
    kEventParamDirectObject = 0x6F626A20   # 'obj '
    typeEventHotKeyID       = 0x686B6964   # 'hkid'
    ID_TOGGLE, ID_CANCEL, ID_LEARN = 1, 2, 3

    def _quien_disparo(event):
        hk = EventHotKeyID()
        err = carbon.GetEventParameter(
            event, c_uint32(kEventParamDirectObject), c_uint32(typeEventHotKeyID),
            None, c_uint32(ctypes.sizeof(hk)), None, byref(hk),
        )
        return hk.id if err == 0 else ID_TOGGLE

    def _on_hotkey(next_handler, event, user_data):
        cual = _quien_disparo(event)
        soltada = carbon.GetEventKind(event) == kEventHotKeyReleased

        if cual == ID_CANCEL:
            if not soltada:
                log(">>> ESCAPE <<<")
                cancel_recording(update_ui=update_ui)
            return 0

        if cual == ID_LEARN:
            if not soltada:
                log(">>> APRENDER <<<")
                threading.Thread(target=aprender_de_portapapeles,
                                 kwargs={"update_ui": update_ui}, daemon=True).start()
            return 0

        # Mantener presionado: pulsar graba, soltar transcribe. Sin esto, el
        # atajo alterna y hay que pulsarlo dos veces.
        if settings.get("push_to_talk", False):
            if soltada:
                log(">>> HOTKEY RELEASED (push to talk) <<<")
                if state["recording"]:
                    threading.Thread(
                        target=stop_and_transcribe,
                        kwargs={"update_ui": update_ui}, daemon=True).start()
            else:
                log(">>> HOTKEY PRESSED (push to talk) <<<")
                if not state["recording"]:
                    start_recording(update_ui=update_ui)
            return 0

        if not soltada:
            log(">>> HOTKEY PRESSED <<<")
            toggle_recording(update_ui=update_ui)
        return 0

    handler_func = EventHandlerProc(_on_hotkey)
    _hotkey_refs.append(handler_func)

    kEventClassKeyboard  = 0x6B657962
    kEventHotKeyPressed  = 5
    # Se registran los DOS eventos: sin el de soltar no hay push to talk.
    tipos = (EventTypeSpec * 2)(
        EventTypeSpec(kEventClassKeyboard, kEventHotKeyPressed),
        EventTypeSpec(kEventClassKeyboard, kEventHotKeyReleased),
    )
    handler_ref = c_void_p()

    err = carbon.InstallEventHandler(
        carbon.GetApplicationEventTarget(), handler_func,
        c_uint32(2), tipos, None, byref(handler_ref),
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

    # Atajo para aprender de una corrección. Este sí es permanente: no graba
    # nada, solo lee el portapapeles cuando lo pulsas.
    if settings.get("aprender_correcciones", True):
        mod_ap = _MODIFIER_MAP.get(
            str(settings.get("hotkey_aprender_mod", "alt")).lower(), 0x0800)
        key_ap = _KEYCODE_MAP.get(
            str(settings.get("hotkey_aprender_key", "l")).lower())
        if key_ap is None:
            log("Atajo de aprender inválido, se omite")
        else:
            ref_ap = c_void_p()
            err_ap = carbon.RegisterEventHotKey(
                c_uint32(key_ap), c_uint32(mod_ap),
                EventHotKeyID(0x51565F31, ID_LEARN),
                carbon.GetApplicationEventTarget(), c_uint32(0), byref(ref_ap),
            )
            if err_ap == 0:
                _hotkey_refs.append(ref_ap)
                log("Atajo para aprender registrado: %s+%s" % (
                    settings.get("hotkey_aprender_mod", "alt"),
                    settings.get("hotkey_aprender_key", "l")))
            else:
                log("RegisterEventHotKey(aprender) failed: %d" % err_ap)

    # Escape vive solo durante la grabación. Registrarlo de forma permanente
    # se lo quitaría a todas las apps del sistema.
    esc = {"ref": None}

    def _reg_esc():
        if esc["ref"] is not None:
            return
        ref = c_void_p()
        err = carbon.RegisterEventHotKey(
            c_uint32(_KEYCODE_MAP["escape"]), c_uint32(0),
            EventHotKeyID(0x51565F31, ID_CANCEL),
            carbon.GetApplicationEventTarget(), c_uint32(0), byref(ref),
        )
        if err == 0:
            esc["ref"] = ref
        else:
            log("RegisterEventHotKey(escape) failed: %d" % err)

    def _lib_esc():
        if esc["ref"] is None:
            return
        carbon.UnregisterEventHotKey(esc["ref"])
        esc["ref"] = None

    _escape_state["registrar"] = _reg_esc
    _escape_state["liberar"] = _lib_esc
    log("Escape disponible para cancelar durante la grabación")

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

        # Escape cancela, pero solo mientras se graba. A diferencia de macOS
        # aquí no hace falta registrar ni liberar nada: pynput es un listener
        # PASIVO, no se apropia de la tecla. La contrapartida es que el Escape
        # también llega a la app que tenga el foco.
        if key == keyboard.Key.esc and state["recording"]:
            log(">>> ESCAPE <<<")
            cancel_recording(update_ui=update_ui)
            return

        mod_held = any(mk in pressed_keys for mk in mod_keys)
        if mod_held and key == main_key:
            if settings.get("push_to_talk", False):
                # Mantener presionado: la tecla se repite mientras se sostiene,
                # así que solo cuenta la primera vez.
                if not state["recording"]:
                    log(">>> HOTKEY PRESSED (push to talk) <<<")
                    start_recording(update_ui=update_ui)
                return
            now = time.time()
            if now - _last_fire[0] > 0.4:   # debounce 400 ms
                _last_fire[0] = now
                log(">>> HOTKEY PRESSED <<<")
                toggle_recording(update_ui=update_ui)

    def on_release(key):
        pressed_keys.discard(key)
        if not settings.get("push_to_talk", False):
            return
        # Soltar el modificador o la tecla principal cierra la grabación.
        if (key == main_key or key in mod_keys) and state["recording"]:
            log(">>> HOTKEY RELEASED (push to talk) <<<")
            threading.Thread(
                target=stop_and_transcribe,
                kwargs={"update_ui": update_ui}, daemon=True).start()

    listener = keyboard.Listener(on_press=on_press, on_release=on_release)
    listener.daemon = True
    listener.start()
    _hotkey_refs.append(listener)
    log(f"pynput hotkey registered: {mod_name}+{key_name}")

# ─── Inicio automático al iniciar sesión ─────────────────────────────────────

BUNDLE_ID = "com.qstrauss.voice"


def _ruta_ejecutable():
    """Qué hay que lanzar al iniciar sesión.

    Empaquetada es el binario dentro del .app; desde código fuente es run.sh,
    que activa el venv antes de arrancar.
    """
    if getattr(sys, "frozen", False):
        return sys.executable
    r = os.path.join(BASE_DIR, "run.sh")
    return r if os.path.exists(r) else sys.executable


def configurar_inicio_automatico(activar):
    """Registra o quita la app del arranque de sesión. True si quedó aplicado."""
    try:
        if IS_MAC:
            destino = os.path.expanduser(
                "~/Library/LaunchAgents/%s.plist" % BUNDLE_ID)
            if not activar:
                if os.path.exists(destino):
                    os.remove(destino)
                    log("Inicio automático desactivado")
                return True
            os.makedirs(os.path.dirname(destino), exist_ok=True)
            plist = (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                '<plist version="1.0"><dict>\n'
                '  <key>Label</key><string>%s</string>\n'
                '  <key>ProgramArguments</key><array><string>%s</string></array>\n'
                '  <key>RunAtLoad</key><true/>\n'
                '</dict></plist>\n' % (BUNDLE_ID, _ruta_ejecutable())
            )
            with open(destino, "w", encoding="utf-8") as f:
                f.write(plist)
            log("Inicio automático activado: %s" % destino)
            return True

        if IS_WINDOWS:
            import winreg
            clave = r"Software\Microsoft\Windows\CurrentVersion\Run"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, clave, 0,
                                winreg.KEY_SET_VALUE) as k:
                if activar:
                    winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ,
                                      '"%s"' % _ruta_ejecutable())
                    log("Inicio automático activado (registro)")
                else:
                    try:
                        winreg.DeleteValue(k, APP_NAME)
                        log("Inicio automático desactivado (registro)")
                    except FileNotFoundError:
                        pass
            return True
    except Exception as e:
        log("No se pudo configurar el inicio automático: %s" % e)
        return False
    return False


# ─── Descarga del modelo por inactividad ─────────────────────────────────────

def _descargar_modelo():
    """Libera el modelo de memoria tras un rato sin dictar.

    En MLX el modelo vive en `ModelHolder`, un cache de clase dentro de
    mlx_whisper. Vaciarlo es transparente: `state["model"]` guarda solo el
    nombre del repo, asi que la siguiente transcripcion lo recarga sola.

    En faster-whisper `state["model"]` ES el objeto, asi que hay que marcarlo
    como descargado para que `start_recording` dispare la recarga y avise al
    usuario mientras tanto.
    """
    import gc
    backend = state.get("backend")
    try:
        if backend == "mlx":
            from mlx_whisper.transcribe import ModelHolder
            ModelHolder.model = None
            ModelHolder.model_path = None
            # Vaciar el ModelHolder no basta: MLX guarda los buffers en un
            # pool de Metal propio. Medido con mx.get_active_memory(): el
            # modelo ocupa 1618 MB y solo baja a 0 tras clear_cache().
            try:
                import mlx.core as mx
                mx.clear_cache()
            except Exception:
                pass
        else:
            state["model"] = None
            state["descargado"] = True
        gc.collect()
        log("Modelo descargado por inactividad (libera ~1.6 GB en MLX)")
    except Exception as e:
        log("No se pudo descargar el modelo: %s" % e)


def _vigilar_inactividad():
    """Hilo de fondo. Revisa cada 15 s si toca liberar el modelo."""
    while True:
        time.sleep(15)
        try:
            espera = int(settings.get("memory_timeout", 0) or 0)
            if espera <= 0:
                continue
            if state["recording"] or state["transcribing"]:
                continue
            ultimo = state.get("ultimo_uso") or 0.0
            if not ultimo:
                continue
            if time.time() - ultimo < espera:
                continue
            ya_libre = (state.get("backend") != "mlx" and state["model"] is None)
            if ya_libre:
                continue
            _descargar_modelo()
            state["ultimo_uso"] = 0.0
        except Exception as e:
            log("Vigilante de inactividad: %s" % e)


# ─── Load model ──────────────────────────────────────────────────────────────

_MLX_MODEL_MAP = {
    "tiny":     "mlx-community/whisper-large-v3-turbo",
    "base":     "mlx-community/whisper-large-v3-turbo",
    "small":    "mlx-community/whisper-large-v3-turbo",
    "medium":   "mlx-community/whisper-large-v3-turbo",
    "turbo":    "mlx-community/whisper-large-v3-turbo",
    "large-v3": "mlx-community/whisper-large-v3-turbo",
}

def load_model():
    model_name = settings.get("whisper_model", "turbo")

    if IS_MAC:
        try:
            import mlx_whisper  # noqa: F401 — confirm it's installed
            repo = _MLX_MODEL_MAP.get(model_name, "mlx-community/whisper-large-v3-turbo")
            log(f"MLX Whisper ready (Metal GPU): {repo}")
            state["model"]   = repo
            state["backend"] = "mlx"
            return
        except ImportError:
            log("mlx-whisper not installed — falling back to faster-whisper CPU")
        except Exception as e:
            log(f"MLX Whisper error — falling back to faster-whisper: {e}")

    # Windows or MLX fallback
    cpu_threads = max(4, os.cpu_count() or 4)
    log(f"Loading faster-whisper '{model_name}' (cpu_threads={cpu_threads})...")
    try:
        state["model"] = WhisperModel(
            model_name,
            device="cpu",
            compute_type="int8",
            cpu_threads=cpu_threads,
            num_workers=1,
        )
        state["backend"] = "faster_whisper"
        state["model_error"] = None
        log("faster-whisper ready (CPU)")
    except Exception as e:
        # Sin esto, load_model() corre en un hilo, la excepción se lo lleva sin
        # dejar rastro, y la app queda inservible respondiendo "Model not ready
        # yet" para siempre sin explicar por qué.
        state["model_error"] = str(e)
        log("ERROR AL CARGAR EL MODELO: %s" % e)
        import traceback
        log(traceback.format_exc())

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

            # No keep-alive window needed — NSStatusItem keeps the run loop alive

            # Audio stream
            try:
                start_audio_stream()
                log("Audio stream OK")
            except Exception as e:
                log(f"Audio stream FAILED: {e}")

            # Overlay (hidden until hotkey)
            self._overlay = RecordingOverlay(
                posicion=settings.get("overlay_position", "center"))
            log("Overlay created")

            # Settings window
            self._settings_win = SettingsWindow(
                on_setting_changed=self._on_setting_changed,
                on_reload_dict=reload_dictionary,
            )
            # La ventana de ajustes roba el foco al abrirse, y el pegado va a
            # donde esté el foco. Con start_hidden la app arranca invisible y
            # el primer dictado aterriza donde el usuario ya estaba.
            if not settings.get("start_hidden", False):
                self._settings_win.show()
                log("Settings window shown")
            else:
                log("start_hidden: ventana de ajustes oculta al arrancar")

            # Load model in background
            threading.Thread(target=load_model, daemon=True).start()
            log("Model loading in background...")
            threading.Thread(target=_vigilar_inactividad, daemon=True).start()

            # Carbon hotkey (no Accessibility needed)
            self._pending_status = None
            self._no_speech_until = 0.0
            try:
                start_hotkey_listener(update_ui=self._queue_status)
            except Exception as e:
                log(f"Hotkey FAILED: {e}")

            # Poll timer: check for status changes from hotkey thread
            NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                0.05, self, b"pollStatus:", None, True
            )
            if not accesibilidad_ok(pedir=True):
                log("FALTA PERMISO DE ACCESIBILIDAD. Sin él la app transcribe "
                    "pero no puede pegar sola. Se abrió el diálogo del sistema.")
            else:
                log("Accesibilidad concedida: el pegado automático funcionará")

            log("App ready")

        def pollStatus_(self, timer):
            status = self._pending_status
            if status is not None:
                self._pending_status = None
                self._apply_status(status)
            elif self._no_speech_until and time.time() >= self._no_speech_until:
                # El aviso "sin voz" se cierra solo. Se reusa este timer de
                # 50 ms en vez de crear uno nuevo: menos piezas móviles y ya
                # corre en el hilo principal, que es donde vive el overlay.
                self._no_speech_until = 0.0
                self._overlay.hide()

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
                self._no_speech_until = 0.0
                self._overlay.show("listening")
            elif status == "transcribing":
                # Always call show() — overlay may be hidden if "recording"
                # status was skipped by the single-slot _pending_status queue
                self._no_speech_until = 0.0
                self._overlay.show("transcribing")
            elif status in ("no_speech", "portapapeles", "sin_permiso", "cargando", "aprendido", "sin_correccion"):
                self._overlay.show(status)
                self._no_speech_until = time.time() + NO_SPEECH_FLASH_S
            else:
                self._no_speech_until = 0.0
                self._overlay.hide()

        @objc.python_method
        def _on_setting_changed(self, key, value):
            global settings
            settings[key] = value
            save_settings(settings)
            if key == "overlay_position":
                self._overlay.set_posicion(value)
            if key == "launch_at_login":
                configurar_inicio_automatico(bool(value))
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
        # Rename process so macOS shows "QStrauss Voice" everywhere
        # (menu bar bold name, Cmd+Tab switcher, dock label)
        import ctypes
        try:
            ctypes.cdll.LoadLibrary("libc.dylib").setprogname(b"QStrauss Voice")
        except Exception:
            pass
        from Foundation import NSProcessInfo, NSBundle
        NSProcessInfo.processInfo().setProcessName_("QStrauss Voice")
        # Patch CFBundleName in the live bundle dict — affects dock label
        try:
            info = NSBundle.mainBundle().infoDictionary()
            info.setValue_forKey_("QStrauss Voice", "CFBundleName")
            info.setValue_forKey_("QStrauss Voice", "CFBundleDisplayName")
            # LSUIElement=1 BEFORE sharedApplication() → macOS never registers
            # this process with the Dock or Cmd+Tab switcher
            info.setValue_forKey_("1", "LSUIElement")
        except Exception:
            pass

        app = AppKit.NSApplication.sharedApplication()
        app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

        # Build complete menu structure BEFORE calling setMainMenu_
        # macOS reads the app name from app_item's submenu title at the moment
        # setMainMenu_ is called — if submenu isn't set yet it falls back to "Python"
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

        app_item = AppKit.NSMenuItem.alloc().init()
        app_item.setSubmenu_(app_menu)   # submenu set BEFORE adding to menubar

        menubar = AppKit.NSMenu.alloc().init()
        menubar.addItem_(app_item)
        app.setMainMenu_(menubar)        # set AFTER full structure is ready

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
            if key == "launch_at_login":
                configurar_inicio_automatico(bool(value))
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
  }else if(status==='no_speech'||status==='portapapeles'||status==='sin_permiso'||status==='cargando'||status==='aprendido'||status==='sin_correccion'){
    ctx.strokeStyle='rgba(140,153,179,.85)';ctx.lineWidth=2;
    ctx.beginPath();ctx.moveTo(14,cy);ctx.lineTo(266,cy);ctx.stroke();
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
  var h={listening:'Presiona el atajo para detener',no_speech:'No se detecto voz',portapapeles:'Copiado, pega con Ctrl+V',sin_permiso:'Falta permiso, pega con Ctrl+V',cargando:'Cargando el modelo, espera',aprendido:'Aprendido',sin_correccion:'Nada que aprender'};
  document.getElementById('hint').textContent=h[s]||'Un momento…';
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
                threading.Thread(target=_vigilar_inactividad, daemon=True).start()
            threading.Thread(target=_load_and_notify, daemon=True).start()

            def queue_status(s):
                _pending["status"] = s
            start_hotkey_listener(update_ui=queue_status)

            # Poll thread: model ready + overlay status
            _no_speech_until = [0.0]

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
                                _no_speech_until[0] = 0.0
                                overlay_win.show()
                                overlay_win.evaluate_js("setStatus('listening')")
                            elif s == "transcribing":
                                _no_speech_until[0] = 0.0
                                overlay_win.evaluate_js("setStatus('transcribing')")
                            elif s in ("no_speech", "portapapeles", "sin_permiso", "cargando", "aprendido", "sin_correccion"):
                                overlay_win.show()
                                overlay_win.evaluate_js("setStatus('%s')" % s)
                                _no_speech_until[0] = time.time() + NO_SPEECH_FLASH_S
                            else:
                                _no_speech_until[0] = 0.0
                                overlay_win.hide()
                        except Exception as e:
                            log(f"overlay error: {e}")
                    elif _no_speech_until[0] and time.time() >= _no_speech_until[0]:
                        _no_speech_until[0] = 0.0
                        try:
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

LOG_FILE = os.path.join(DATA_DIR, "app.log")

def log(msg):
    """Write to both stdout and log file for debugging."""
    print(msg, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")

LOCK_FILE = os.path.join(DATA_DIR, "app.lock")

def _acquire_lock():
    """Instancia única. False si ya hay otra corriendo.

    En macOS esto devolvía True sin mirar nada, así que se podían abrir
    instancias infinitas. Dos instancias responden al MISMO atajo global y se
    pelean el micrófono: una arranca a grabar mientras la otra para, y la que
    pierde el micro captura silencio puro. Visto en el log como dos
    "HOTKEY PRESSED" en el mismo segundo con estados opuestos, grabaciones con
    peak=0.0000 y una de 127 s por un toggle desincronizado. Se siente como
    lentitud, pero no transcribe nada.

    El lock es un archivo con flock: el bloqueo lo libera el sistema cuando el
    proceso muere, incluso si lo matan, así que no quedan locks huérfanos como
    pasaría comprobando un PID guardado.
    """
    if not IS_WINDOWS:
        import fcntl
        try:
            f = open(LOCK_FILE, "w")
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            f.write(str(os.getpid()))
            f.flush()
            _hotkey_refs.append(f)   # mantener vivo el descriptor
            return True
        except (IOError, OSError):
            return False
        except Exception as e:
            log("No se pudo tomar el lock: %s" % e)
            return True   # ante la duda, dejar arrancar
    import ctypes
    mutex = ctypes.windll.kernel32.CreateMutexW(None, True, "QStraussVoice_SingleInstance")
    err = ctypes.windll.kernel32.GetLastError()
    if err == 183:  # ERROR_ALREADY_EXISTS
        return False
    _hotkey_refs.append(mutex)  # keep reference alive
    return True

def main():
    if not _acquire_lock():
        # El aviso tiene que ser visible: la app arranca oculta, asi que un
        # segundo arranque que solo escribe en el log parece que no hizo nada.
        aviso = "%s ya está corriendo." % APP_NAME
        print(aviso)
        if IS_WINDOWS:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, aviso, APP_NAME, 0x40)
        else:
            try:
                subprocess.run([
                    "osascript", "-e",
                    'display notification "%s" with title "%s"' % (aviso, APP_NAME)
                ], timeout=5)
            except Exception:
                pass
        return

    # Clear log
    with open(LOG_FILE, "w") as f:
        f.write("")
    log(f"{APP_NAME} starting...")
    log(f"Hotkey: {settings.get('hotkey_display', '⌥ Space')}")

    reload_dictionary()

    # Sincronizar el inicio automático con lo que dice el ajuste. Sin esto,
    # el estado real y el declarado se separan en silencio: basta que algo
    # borre el plist (o la entrada de registro) para que el interruptor siga
    # en "activado" sin estarlo.
    if settings.get("launch_at_login", False):
        configurar_inicio_automatico(True)

    if IS_MAC:
        run_mac()
    elif IS_WINDOWS:
        run_windows()

if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    main()
