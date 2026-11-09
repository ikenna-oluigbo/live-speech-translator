"""
Live speech translation broadcast (English <-> French), 100% local and free.

Pipeline:
  Audio sound/Mixer -> laptop audio input -> speech detection -> Whisper (text)
        -> Ollama (translated text) -> Piper (voice) -> WebSocket -> listeners' phones

Usage:
  python app.py --list     show audio devices (find your mixer)
  python app.py --meter    show live input level (to set SPEECH_THRESHOLD)
  python app.py            run the whole system
"""
import argparse
import asyncio
import collections
import io
import json
import os
import queue
import socket
import threading
import time
import wave
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
import sounddevice as sd

# =====================================================================
#  SETTINGS - Edit as it suits your needs
# =====================================================================
DIRECTION = "en->fr"         # "en->fr" (English speaker) or "fr->en" (French speaker)

AUDIO_DEVICE = 1          # None = Windows default input. Or put the number from --list
CHANNELS = 1                 # If you get an error, try 2 (the app mixes them to mono)
SPEECH_THRESHOLD = 0.05      # Volume level that counts as "speaking". Tune with --meter
PAUSE_SECONDS = 0.5          # Silence that ends a sentence. Lower = faster, but may cut sentences
LONG_TALK_SECONDS = 4        # After this much non-stop talking, accept even shorter pauses
MAX_SECONDS = 10             # Force a cut if someone talks this long without any pause

WHISPER_MODEL = "small.en"   # "base.en", "small.en", "medium.en" (English only) or "large-v3-turbo" (any language) || "small", "medium" (French Input)
WHISPER_DEVICE = "cpu"       # "cpu" or "cuda" (NVIDIA GPU)
WHISPER_COMPUTE = "int8"     # cpu: "int8"   |   cuda: "int8_float16" (less memory) or "float16"
WHISPER_CPU_THREADS = 0      # 0 = automatic. On CPU you can try 6 or 8 if your laptop has many cores

OLLAMA_MODEL = "gemma3:4b"   # Must appear in 'ollama list'. Others: "llama3.2:3b", "qwen2.5:7b", "mistral:7b"
OLLAMA_URL = None            # None = auto-detect from OLLAMA_HOST (recommended)

# DEFAULT voices. To choose/download/listen to other voices, run:  python voices.py
# (your choice is saved in voice_settings.json and overrides these defaults)
PIPER_VOICES = {
    "fr": "voices/fr_FR-siwis-medium.onnx",
    "en": "voices/en_US-lessac-medium.onnx",
}
PORT = 8000

# Extra instructions for the translator (names, terms to keep, tone...). Example:
# EXTRA_INSTRUCTIONS = "Keep the name 'Artificial Intelligence' untranslated. Use formal French (vous)."
EXTRA_INSTRUCTIONS = ""
# =====================================================================

SAMPLE_RATE = 16000
BLOCK_SECONDS = 0.1
BASE_DIR = Path(__file__).parent

# Smaller context + capped answer length = faster translations (same values are used for
# the warm-up call; changing them between calls would force Ollama to reload the model)
OLLAMA_OPTIONS = {"temperature": 0.1, "num_ctx": 2048, "num_predict": 300}
OLLAMA_KEEP_ALIVE = "4h"

SOURCE, TARGET = [x.strip() for x in DIRECTION.split("->")]
if SOURCE not in ("en", "fr") or TARGET not in ("en", "fr") or SOURCE == TARGET:
    raise SystemExit('DIRECTION must be "en->fr" or "fr->en"')
LANG_NAMES = {"en": "English", "fr": "French"}

audio_q = queue.Queue()      # (timestamp, audio block) from the mixer
text_q = queue.Queue()       # (text, time speech ended) waiting to be translated
clients = set()              # connected listener websockets
main_loop = None             # asyncio loop of the web server
whisper_ready = threading.Event()
_ready_parts = set()
_ready_lock = threading.Lock()


def mark_ready(part):
    """Print a clear READY banner once both Whisper and the translator are warmed up."""
    with _ready_lock:
        _ready_parts.add(part)
        if _ready_parts == {"whisper", "translator"}:
            print("\n" + "=" * 60)
            print(" READY - the system is listening. You can start speaking.")
            print("=" * 60 + "\n")


def rms(block):
    return float(np.sqrt(np.mean(np.square(block))))


def audio_callback(indata, frames, time_info, status):
    if status:
        print("Audio warning:", status)
    audio_q.put((time.time(), indata.mean(axis=1).copy()))  # mix to mono


def open_mic():
    return sd.InputStream(
        device=AUDIO_DEVICE, channels=CHANNELS, samplerate=SAMPLE_RATE,
        blocksize=int(SAMPLE_RATE * BLOCK_SECONDS), dtype="float32",
        callback=audio_callback,
    )


def run_safely(fn, label):
    """Run a background part; if it crashes, say so loudly instead of dying silently."""
    try:
        fn()
    except Exception as e:
        import traceback
        traceback.print_exc()
        print("\n" + "!" * 60)
        print(f"!! The {label} part CRASHED and stopped: {e}")
        print("!! Nothing will be translated until this is fixed. Fix it, then restart.")
        print("!! Tip: run  python check_setup.py  to find what is missing.")
        print("!" * 60 + "\n")


# ---------------------------------------------------------------------
# Whisper loading (GPU with safe fallbacks)
# ---------------------------------------------------------------------
def add_cuda_dlls():
    """Let Windows find the NVIDIA libraries installed with pip (fixes any cuBLAS/cuDNN 'not found' error)."""
    import glob
    import site
    folders = []
    for base in site.getsitepackages() + [site.getusersitepackages()]:
        folders += glob.glob(os.path.join(base, "nvidia", "*", "bin"))
    for d in folders:
        try:
            os.add_dll_directory(d)
        except Exception:
            pass
        os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")


def _model_name(m):
    if isinstance(m, dict):
        return m.get("model") or m.get("name")
    return getattr(m, "model", None) or getattr(m, "name", None)


def free_ollama_vram():
    """Unload any model Ollama is still holding in GPU memory, so Whisper can load first.
    If you are using an Ollama model for other unrelated tasks, this stops and unload the model automatically. You can rerun the stopped model
    (A model left over from the previous run is a common cause of GPU 'out of memory' / cuBLAS errors.)"""
    try:
        from ollama_connect import get_client
        client = get_client(OLLAMA_URL)
        running = client.ps()
        models = running["models"] if isinstance(running, dict) else running.models
        for m in models:
            name = _model_name(m)
            if name:
                client.generate(model=name, prompt="", keep_alive=0)
                print(f"   (Unloaded '{name}' from GPU memory so Whisper can load first)")
    except Exception:
        pass   # Ollama not running yet, or nothing loaded: nothing to free


def load_whisper(name):
    """Load Whisper and warm it up. On GPU: try several memory modes, then fall back to CPU."""
    import gc
    from faster_whisper import WhisperModel

    threads = WHISPER_CPU_THREADS or max(4, (os.cpu_count() or 8) // 2)

    def try_load(device, compute, model_name):
        model = WhisperModel(model_name, device=device, compute_type=compute, cpu_threads=threads)
        # Warm-up: GPU problems often only appear on first real use. Also makes sentence 1 faster.
        segments, _ = model.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32),
                                       language=SOURCE, beam_size=1)
        list(segments)
        return model

    if WHISPER_DEVICE != "cuda":
        return try_load("cpu", WHISPER_COMPUTE, name)

    free_ollama_vram()
    add_cuda_dlls()
    for compute in dict.fromkeys([WHISPER_COMPUTE, "int8_float16", "float16", "int8"]):
        try:
            model = try_load("cuda", compute, name)
            if compute != WHISPER_COMPUTE:
                print(f"   (The GPU works with '{compute}'. Set WHISPER_COMPUTE = \"{compute}\" to skip the retries.)")
            return model
        except Exception as e:
            print(f"\n!! GPU load failed with compute_type='{compute}':\n   {e}")
            gc.collect()

    fallback = "small.en" if SOURCE == "en" else "small"
    print(f"\n!! The GPU could not be used for Whisper. Falling back to CPU with '{fallback}'.")
    print("!! Run  python check_setup.py  to diagnose the GPU.\n")
    return try_load("cpu", "int8", fallback)


# ---------------------------------------------------------------------
# STEP 1+2: listen to the audio source/mixer, cut into sentences, transcribe
# ---------------------------------------------------------------------
def listen_and_transcribe():
    whisper_name = WHISPER_MODEL
    if SOURCE != "en" and whisper_name.endswith(".en"):
        whisper_name = whisper_name[:-3]   # ".en" models only understand English
        print(f"   (French input needs a multilingual model, using '{whisper_name}' instead)")
    print(f"[1/3] Loading Whisper model '{whisper_name}' on {WHISPER_DEVICE.upper()} "
          f"({WHISPER_COMPUTE}), listening for {LANG_NAMES[SOURCE]} (first time = download)...")
    try:
        model = load_whisper(whisper_name)
    finally:
        whisper_ready.set()     # lets the translator start loading (even if Whisper failed)
    print("[1/3] Whisper ready.")
    mark_ready("whisper")

    pause_blocks = max(1, round(PAUSE_SECONDS / BLOCK_SECONDS))
    short_pause_blocks = max(2, pause_blocks // 2)
    preroll = collections.deque(maxlen=3)   # keep a little audio from before speech starts
    buf, speaking, quiet, loud_count = [], False, 0, 0
    last_loud_t = time.time()
    last_lag_warning = 0.0

    with open_mic():
        while True:
            block_t, block = audio_q.get()
            lag = time.time() - block_t
            if lag > 3 and time.time() - last_lag_warning > 10:
                last_lag_warning = time.time()
                print(f"  !! Audio is being processed {lag:.0f}s late (transcription can't keep up)")

            loud = rms(block) > SPEECH_THRESHOLD
            if loud:
                last_loud_t = block_t

            if not speaking:
                if loud:
                    speaking, quiet, loud_count = True, 0, 1
                    buf = list(preroll) + [block]
                else:
                    preroll.append(block)
                continue

            buf.append(block)
            loud_count += 1 if loud else 0
            quiet = 0 if loud else quiet + 1
            seconds = len(buf) * BLOCK_SECONDS
            needed = pause_blocks if seconds < LONG_TALK_SECONDS else short_pause_blocks

            if quiet >= needed or seconds >= MAX_SECONDS:
                audio = np.concatenate(buf)
                enough_speech = loud_count >= 3
                buf, speaking, quiet, loud_count = [], False, 0, 0
                preroll.clear()
                if not enough_speech:
                    continue  # just a click or bump, ignore

                t0 = time.time()
                segments, _ = model.transcribe(
                    audio, language=SOURCE, beam_size=1,
                    temperature=0.0,              # no slow "retry" decoding
                    without_timestamps=True,      # we don't need timestamps
                    vad_filter=True, condition_on_previous_text=False,
                )
                text = " ".join(s.text.strip() for s in segments).strip()
                if text:
                    print(f"\n{SOURCE.upper()} ({time.time() - t0:.1f}s to transcribe): {text}")
                    text_q.put((text, last_loud_t))


# ---------------------------------------------------------------------
# STEP 3+4: translate with Ollama, speak with Piper, send to listeners
# ---------------------------------------------------------------------
def make_wav(voice, text):
    pcm, rate = bytearray(), None
    if hasattr(voice, "synthesize_stream_raw"):        # older piper-tts
        rate = voice.config.sample_rate
        for chunk in voice.synthesize_stream_raw(text):
            pcm += chunk
    else:                                              # newer piper-tts
        for chunk in voice.synthesize(text):
            rate = chunk.sample_rate
            pcm += chunk.audio_int16_bytes
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(pcm))
    return out.getvalue()


async def broadcast(text, wav):
    dead = []
    for ws in list(clients):
        try:
            await ws.send_text(json.dumps({"fr": text}))   # key name is historical; holds the translation
            await ws.send_bytes(wav)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)


SYSTEM_PROMPT = (
    f"You are a professional live interpreter. Translate the user's {LANG_NAMES[SOURCE]} speech "
    f"into natural, spoken {LANG_NAMES[TARGET]}. Output ONLY the {LANG_NAMES[TARGET]} translation: "
    "no quotes, no notes, no explanations. If the text is a sentence fragment, translate it as it is. "
    + EXTRA_INSTRUCTIONS
)


def chosen_voices():
    """Voices to use: the defaults above, overridden by the choice saved with voices.py."""
    voices = dict(PIPER_VOICES)
    try:
        saved = json.loads((BASE_DIR / "voice_settings.json").read_text(encoding="utf-8"))
        voices.update({k: v for k, v in saved.items() if k in ("en", "fr") and v})
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"!! Could not read voice_settings.json ({e}). Using the default voices.")
    return voices


def translate_and_speak():
    from piper import PiperVoice
    from ollama_connect import get_client, resolve_url, model_names

    voice_file = chosen_voices()[TARGET]
    if not (BASE_DIR / voice_file).exists():
        raise FileNotFoundError(f"Voice file '{voice_file}' not found. "
                                "Run  python voices.py  to download or choose a voice.")
    print(f"[2/3] Loading {LANG_NAMES[TARGET]} voice '{Path(voice_file).stem}'...")
    voice = PiperVoice.load(str(BASE_DIR / voice_file))
    make_wav(voice, "Test.")   # warm-up so the first real sentence isn't slow
    print("[2/3] Voice ready.")

    ollama_address = resolve_url(OLLAMA_URL)
    print(f"[3/3] Connecting to Ollama at {ollama_address} (model '{OLLAMA_MODEL}')...")
    client = get_client(OLLAMA_URL)   # works with OLLAMA_HOST set to 0.0.0.0 or not set at all
    try:
        installed = model_names(client)
        print("[3/3] Ollama connection OK.")
        if OLLAMA_MODEL not in installed:
            print(f"!! Model '{OLLAMA_MODEL}' is not installed. Run: ollama pull {OLLAMA_MODEL}")
            print("   Installed models:", ", ".join(installed) or "(none)")
    except Exception as e:
        print(f"!! Cannot reach Ollama at {ollama_address}: {e}")

    # Wait until Whisper is loaded, THEN load the translator into GPU memory.
    # (Loading both at once makes them fight for GPU memory.)
    whisper_ready.wait(timeout=600)
    print(f"[3/3] Warming up the translator '{OLLAMA_MODEL}' (loads it into memory)...")
    t0 = time.time()
    try:
        client.chat(
            model=OLLAMA_MODEL,
            messages=[{"role": "system", "content": SYSTEM_PROMPT},
                      {"role": "user", "content": "Hello."}],
            options=OLLAMA_OPTIONS, keep_alive=OLLAMA_KEEP_ALIVE,
        )
        print(f"[3/3] Translator ready ({time.time() - t0:.1f}s).")
    except Exception as e:
        print(f"!! Translator warm-up failed: {e}")
    mark_ready("translator")

    history = collections.deque(maxlen=2)   # last sentences, for context

    while True:
        first_text, speech_end_t = text_q.get()
        texts = [first_text]
        # If sentences piled up, translate them together to catch up
        while True:
            try:
                texts.append(text_q.get_nowait()[0])
            except queue.Empty:
                break
        if len(texts) > 1:
            print(f"  (catching up: translating {len(texts)} waiting sentences together)")
        source_text = " ".join(texts)

        t0 = time.time()
        try:
            user_msg = source_text
            if history:
                user_msg = (f"(Earlier context, do NOT translate it: {' '.join(history)})\n\n"
                            f"Translate this: {source_text}")
            reply = client.chat(
                model=OLLAMA_MODEL,
                messages=[{"role": "system", "content": SYSTEM_PROMPT},
                          {"role": "user", "content": user_msg}],
                options=OLLAMA_OPTIONS, keep_alive=OLLAMA_KEEP_ALIVE,
            )
            translated = reply["message"]["content"].strip().strip('"«» ')
            history.append(source_text)
            t1 = time.time()
            wav = make_wav(voice, translated)
            t2 = time.time()
            asyncio.run_coroutine_threadsafe(broadcast(translated, wav), main_loop).result(timeout=30)
            print(f"{TARGET.upper()} ({t1 - t0:.1f}s translate + {t2 - t1:.1f}s voice): {translated}")
            print(f"   >> Delay until the voice STARTS on listeners' phones: "
                  f"{time.time() - speech_end_t:.1f}s after the speaker stopped")
        except Exception as e:
            print("!! Translation/voice error:", e)


# ---------------------------------------------------------------------
# Web server (serves the listener page + audio over WebSocket)
# ---------------------------------------------------------------------
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse


@asynccontextmanager
async def lifespan(app):
    global main_loop
    main_loop = asyncio.get_running_loop()
    threading.Thread(target=run_safely, args=(listen_and_transcribe, "listening/transcription"), daemon=True).start()
    threading.Thread(target=run_safely, args=(translate_and_speak, "translation/voice"), daemon=True).start()
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/image")
def image():
    from fastapi.responses import Response
    image_path = BASE_DIR / "translate.avif"
    if not image_path.exists():
        return {"error": "Image translate.avif not found at: " + str(image_path)}
    with open(image_path, "rb") as f:
        return Response(f.read(), media_type="image/avif")


@app.get("/config")
def config():
    return {"lang": TARGET}   # language the listeners will hear: "fr" or "en"


@app.websocket("/ws")
async def listener(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    print(f"Listener connected ({len(clients)} online)")
    try:
        while True:
            await ws.receive_text()   # keeps the connection open
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(ws)
        print(f"Listener left ({len(clients)} online)")


# ---------------------------------------------------------------------
# Helpers + command line
# ---------------------------------------------------------------------
def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def show_meter():
    print("Speak into the mic. Watch the number. Press Ctrl+C to stop.")
    print("Set SPEECH_THRESHOLD between the 'silence' value and the 'speaking' value.\n")
    with open_mic():
        while True:
            level = rms(audio_q.get()[1])
            bar = "#" * min(60, int(level * 600))
            print(f"\r{level:.4f} {bar:<60}", end="", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true", help="list audio devices")
    parser.add_argument("--meter", action="store_true", help="show input level")
    args = parser.parse_args()

    if args.list:
        print(sd.query_devices())
        print("\nPut the number of your mixer input in AUDIO_DEVICE at the top of app.py")
    elif args.meter:
        show_meter()
    else:
        import uvicorn

        url = f"http://{local_ip()}:{PORT}"
        print("=" * 60)
        print(" Listeners open this address on their phone:")
        print(f"   {url}")
        print("=" * 60)
        print(" Loading models... wait for the READY message before speaking.\n")
        try:
            import qrcode
            qrcode.make(url).save(BASE_DIR / "join_qr.png")
            print(" (A QR code was saved as join_qr.png - print it or show it on screen)")
        except Exception:
            pass
        uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
